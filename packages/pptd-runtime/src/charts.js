/** PPTD v2 charts. Validation never mutates the authored document. */
import * as echarts from 'echarts';

export const CHART_PALETTE = [
	'#1783FE',
	'#67B8A6',
	'#C9A869',
	'#8A84C7',
	'#8C98A8',
	'#5E9CF4',
	'#7AB5D7',
	'#B08A9C',
	'#333333',
	'#DCDCDC',
];
const TYPES = [
	'bar',
	'line',
	'area',
	'scatter',
	'bubble',
	'candlestick',
	'pie',
	'radar',
	'waterfall',
	'heatmap',
	'treemap',
	'sunburst',
	'sankey',
];
const MULTI = ['bar', 'line', 'area', 'scatter', 'bubble', 'candlestick', 'radar'];
const XY = ['bar', 'line', 'area', 'scatter', 'bubble', 'candlestick', 'waterfall', 'heatmap'];
const LINEAR = ['line', 'area', 'radar'];
const CHANNELS = {
	bar: ['x', 'y'],
	line: ['x', 'y'],
	area: ['x', 'y'],
	scatter: ['x', 'y'],
	bubble: ['x', 'y', 'size'],
	candlestick: ['x', 'high', 'low', 'close'],
	pie: ['category', 'value'],
	radar: ['category', 'y'],
	waterfall: ['x', 'y'],
	heatmap: ['x', 'y', 'value'],
	treemap: ['category', 'value'],
	sunburst: ['category', 'value'],
	sankey: ['source', 'target', 'flow'],
};
const OPTIONAL = {
	candlestick: ['open'],
	waterfall: ['isTotal'],
	treemap: ['parent'],
	sunburst: ['parent'],
};
const isObject = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);
const clone = (x) => structuredClone(x);
const uniq = (x) => [...new Set(x)];
const config = (x) => (isObject(x) ? x : {});
const enabled = (x, fallback = true) =>
	x === undefined ? fallback : x !== false && x?.show !== false;
const merge = (a = {}, b = {}) =>
	Object.fromEntries(
		uniq([...Object.keys(a), ...Object.keys(b)]).map((k) => [
			k,
			isObject(a[k]) && isObject(b[k])
				? { ...a[k], ...b[k] }
				: b[k] !== undefined
					? b[k]
					: a[k],
		]),
	);
function fail(code, path, message) {
	const e = new Error(`${code}: ${path}: ${message}`);
	e.name = code;
	e.path = path;
	throw e;
}
function numeric(value, path) {
	if (value === null) return null;
	if (
		typeof value === 'string' &&
		value.trim() !== '' &&
		/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim())
	)
		value = Number(value);
	if (typeof value !== 'number' || !Number.isFinite(value))
		fail('NonNumericValueError', path, 'expected a finite number or numeric string');
	return value;
}
function checkEnum(value, choices, path) {
	if (value !== undefined && !choices.includes(value))
		fail('InvalidValueError', path, `expected ${choices.join(' | ')}`);
}
function checkRange(value, min, max, path, inclusive = true) {
	if (
		value !== undefined &&
		(typeof value !== 'number' ||
			!Number.isFinite(value) ||
			(inclusive ? value < min : value <= min) ||
			value > max)
	)
		fail('InvalidValueError', path, `expected ${inclusive ? '[' : '('}${min}, ${max}]`);
}
function axisConfig(element, side, index = 0) {
	const value = element[side + 'Axis'];
	return (Array.isArray(value) ? value[index] : value) || {};
}
function inferAxis(element, s, side, rows) {
	const explicit = axisConfig(element, side, s[side + 'AxisIndex'] || 0).type;
	if (explicit) return explicit;
	if (s.type === 'heatmap' || (s.type === 'candlestick' && side === 'x')) return 'category';
	if (['scatter', 'bubble'].includes(s.type)) return 'value';
	const value = rows
		.map((r) => r[element.data.cols.indexOf(s.encode[side])])
		.find((v) => v !== null);
	return typeof value === 'string' ? 'category' : 'value';
}
function topological(names, edges, path) {
	const indegree = new Map(names.map((n) => [n, 0])),
		next = new Map(names.map((n) => [n, []]));
	for (const [a, b] of edges) {
		next.get(a).push(b);
		indegree.set(b, indegree.get(b) + 1);
	}
	const queue = names.filter((n) => indegree.get(n) === 0),
		result = [];
	while (queue.length) {
		const n = queue.shift();
		result.push(n);
		for (const m of next.get(n)) {
			indegree.set(m, indegree.get(m) - 1);
			if (!indegree.get(m)) queue.push(m);
		}
	}
	if (result.length !== names.length)
		fail('CyclicGraphError', path, 'cycle in node relationships');
	return result;
}

export function validateChart(element) {
	const path = element.elementId || 'chart',
		data = element.data;
	if (!data || !Array.isArray(data.cols) || !Array.isArray(data.rows))
		fail('InvalidValueError', path + '.data', 'cols and rows are required');
	if (data.cols.some((c) => typeof c !== 'string' || !c.trim()))
		fail('EmptyColumnError', path + '.data.cols', 'column names must be nonempty strings');
	if (uniq(data.cols).length !== data.cols.length)
		fail('DuplicateColumnError', path + '.data.cols', 'column names must be unique');
	for (const [i, row] of data.rows.entries())
		if (!Array.isArray(row) || row.length !== data.cols.length)
			fail('RowLengthError', `${path}.data.rows[${i}]`, 'row width differs from cols');
	if (!Array.isArray(element.series) || !element.series.length)
		fail('InvalidValueError', path + '.series', 'at least one series is required');
	for (const [type, defaults] of Object.entries(element.seriesDefaults || {})) {
		if (
			!MULTI.includes(type) ||
			!isObject(defaults) ||
			'type' in defaults ||
			'encode' in defaults
		)
			fail(
				'InvalidValueError',
				path + '.seriesDefaults.' + type,
				'only multiseries styles; type and encode are forbidden',
			);
	}
	checkRange(element.barWidth, 0, 1, path + '.barWidth', false);
	for (const k of ['barGap', 'categoryGap'])
		if (
			element[k] !== undefined &&
			(element[k] < 0 ||
				element[k] >= 1 ||
				typeof element[k] !== 'number' ||
				!Number.isFinite(element[k]))
		)
			fail('InvalidValueError', path + '.' + k, 'expected [0,1)');
	const series = element.series.map((s, i) => {
		const loc = `${path}.series[${i}]`;
		checkEnum(s.type, TYPES, loc + '.type');
		if (!TYPES.includes(s.type))
			fail('InvalidValueError', loc + '.type', 'series type is required');
		const result = clone(merge(element.seriesDefaults?.[s.type], s));
		if (!isObject(result.encode))
			fail('InvalidValueError', loc + '.encode', 'encode is required');
		for (const ch of CHANNELS[s.type])
			if (!(ch in result.encode))
				fail('UnknownColumnError', loc + '.encode.' + ch, 'required channel is missing');
		for (const [ch, col] of Object.entries(result.encode)) {
			if (![...CHANNELS[s.type], ...(OPTIONAL[s.type] || [])].includes(ch))
				fail('InvalidValueError', loc + '.encode.' + ch, 'unknown channel');
			if (!data.cols.includes(col))
				fail('UnknownColumnError', loc + '.encode.' + ch, `unknown column ${col}`);
		}
		if (
			result.dataFilter &&
			(!['scatter', 'bubble'].includes(s.type) || !data.cols.includes(result.dataFilter.col))
		)
			fail(
				'UnknownColumnError',
				loc + '.dataFilter.col',
				'filter requires a known column on scatter/bubble',
			);
		for (const side of ['x', 'y']) {
			const index = result[side + 'AxisIndex'] ?? 0,
				axes = element[side + 'Axis'];
			if (
				!Number.isInteger(index) ||
				index < 0 ||
				(index > 0 && (!Array.isArray(axes) || axes.length <= index))
			)
				fail(
					'AxisIndexError',
					loc + '.' + side + 'AxisIndex',
					'index needs a corresponding axis array',
				);
		}
		const rows = data.rows
			.map((row, index) => ({ row, index }))
			.filter(
				({ row }) =>
					!result.dataFilter ||
					row[data.cols.indexOf(result.dataFilter.col)] === result.dataFilter.value,
			);
		result.xType = inferAxis(
			element,
			result,
			'x',
			rows.map((r) => r.row),
		);
		result.yType = inferAxis(
			element,
			result,
			'y',
			rows.map((r) => r.row),
		);
		result.horizontal =
			['bar', 'waterfall', 'line', 'area'].includes(s.type) &&
			result.yType === 'category' &&
			(result.xType === 'value' ||
				axisConfig(element, 'y', result.yAxisIndex || 0).type === 'category');
		// Numeric channel strings remain numeric even when the default y inference sees a string.
		if (
			!result.horizontal &&
			['bar', 'line', 'area', 'waterfall', 'candlestick'].includes(s.type)
		)
			result.yType = 'value';
		if (result.horizontal) result.xType = 'value';
		if (s.type === 'scatter' && result.marker === false)
			fail('InvalidValueError', loc + '.marker', 'scatter markers cannot be disabled');
		checkEnum(
			result.stack,
			s.type === 'area'
				? ['value', 'percent', 'stream']
				: s.type === 'bar'
					? ['value', 'percent']
					: [],
			loc + '.stack',
		);
		checkEnum(result.nullHandling, ['zero', 'gap', 'connect'], loc + '.nullHandling');
		checkEnum(result.lineStyle, ['solid', 'dash', 'dot'], loc + '.lineStyle');
		checkEnum(
			config(result.marker).shape,
			['circle', 'rect', 'diamond', 'triangle'],
			loc + '.marker.shape',
		);
		checkRange(result.width, 0, Infinity, loc + '.width');
		checkRange(config(result.marker).size, 0, Infinity, loc + '.marker.size');
		checkEnum(result.sizeScale, ['linear', 'sqrt', 'log'], loc + '.sizeScale');
		checkEnum(result.nodeAlign, ['left', 'right', 'justify'], loc + '.nodeAlign');
		checkRange(result.innerRadius, 0, 1, loc + '.innerRadius');
		if (result.levels !== undefined && (!Number.isInteger(result.levels) || result.levels < 1))
			fail('InvalidValueError', loc + '.levels', 'expected positive integer');
		if (
			result.sizeRange &&
			(!Array.isArray(result.sizeRange) ||
				result.sizeRange.length !== 2 ||
				!result.sizeRange.every((v) => typeof v === 'number' && Number.isFinite(v)) ||
				result.sizeRange[0] < 0 ||
				result.sizeRange[1] < result.sizeRange[0])
		)
			fail('InvalidValueError', loc + '.sizeRange', 'expected ordered nonnegative radii');
		result.dataLabels = { ...(element.dataLabels || {}), ...(result.dataLabels || {}) };
		checkEnum(
			result.dataLabels.position,
			['top', 'bottom', 'left', 'right', 'inside', 'insideEnd', 'outsideEnd'],
			loc + '.dataLabels.position',
		);
		const contents =
			s.type === 'pie'
				? ['value', 'percentage', 'category']
				: ['waterfall', 'treemap', 'sunburst', 'sankey'].includes(s.type)
					? ['value', 'category']
					: ['value'];
		checkEnum(result.dataLabels.content, contents, loc + '.dataLabels.content');
		for (const fmt of [result.dataLabels.numberFormat])
			if (fmt && !/^(0(?:\.0+)?%?|#,##0|0\.0E\+00)$/.test(fmt))
				fail(
					'InvalidValueError',
					loc + '.dataLabels.numberFormat',
					'unsupported number format',
				);
		result.name ??= result.encode.y || result.encode.value || s.type;
		result.points = rows.map(({ row, index }) => {
			const p = { row: index };
			for (const [ch, col] of Object.entries(result.encode)) {
				const value = row[data.cols.indexOf(col)],
					cell = `${path}.data.rows[${index}][${data.cols.indexOf(col)}]`;
				if (ch === 'isTotal') {
					if (value !== null && typeof value !== 'boolean')
						fail('InvalidValueError', cell, 'isTotal only accepts boolean/null');
					p[ch] = value;
				} else if (
					['value', 'open', 'high', 'low', 'close', 'size', 'flow'].includes(ch) ||
					(ch === 'y' && s.type !== 'heatmap' && !result.horizontal) ||
					(ch === 'x' &&
						(result.horizontal ||
							['scatter', 'bubble'].includes(s.type) ||
							result.xType === 'value'))
				)
					p[ch] = numeric(value, cell);
				else {
					if (value !== null && !['string', 'number'].includes(typeof value))
						fail('InvalidValueError', cell, 'expected category/name');
					p[ch] = value;
				}
			}
			if (s.type === 'heatmap' && (typeof p.x !== 'string' || typeof p.y !== 'string'))
				fail(
					'InvalidValueError',
					loc + '.encode',
					'heatmap x and y must be string categories',
				);
			if (s.type === 'bubble' && p.size !== null) p.size = Math.max(0, p.size);
			if (['pie', 'treemap', 'sunburst'].includes(s.type) && p.value < 0)
				fail(
					'InvalidValueError',
					loc + '.encode.value',
					'negative area values are not representable',
				);
			if (
				s.type === 'sankey' &&
				(typeof p.source !== 'string' ||
					!p.source ||
					typeof p.target !== 'string' ||
					!p.target ||
					p.flow < 0)
			)
				fail(
					'InvalidValueError',
					loc + '.encode',
					'sankey requires names and nonnegative flows',
				);
			return p;
		});
		return result;
	});
	const types = uniq(series.map((s) => s.type));
	if (
		(types.includes('radar') && types.length > 1) ||
		(types.some((t) => !MULTI.includes(t)) && series.length > 1) ||
		(types.includes('candlestick') &&
			types.some((t) => !['bar', 'line', 'area', 'candlestick'].includes(t)))
	)
		fail(
			'TypeMixingError',
			path + '.series',
			'incompatible series types or singleton constraint',
		);
	for (const type of ['bar', 'area'])
		if (uniq(series.filter((s) => s.type === type && s.stack).map((s) => s.stack)).length > 1)
			fail(
				'StackModeMismatchError',
				path + '.series',
				'all stacked series of a type must share a mode',
			);
	if (types.includes('radar') && uniq(series.map((s) => s.encode.category)).length !== 1)
		fail(
			'InvalidValueError',
			path + '.series.encode.category',
			'radar series must share category column',
		);
	const nullHandling =
		series.find((s) => LINEAR.includes(s.type) && s.nullHandling)?.nullHandling ||
		(types.includes('radar') ? 'connect' : 'gap');
	const warnings = [];
	for (const s of series) {
		if (LINEAR.includes(s.type)) {
			s.nullHandling = nullHandling;
			if (nullHandling === 'zero')
				for (const p of s.points)
					if (p[s.horizontal ? 'x' : 'y'] === null) p[s.horizontal ? 'x' : 'y'] = 0;
		}
		if (['treemap', 'sunburst'].includes(s.type)) {
			const names = s.points.map((p) => p.category);
			if (
				names.some((n) => typeof n !== 'string' || !n) ||
				uniq(names).length !== names.length
			)
				fail(
					'InvalidValueError',
					path + '.data',
					'tree node names must be nonempty and unique',
				);
			const edges = s.points
				.filter((p) => p.parent !== null && p.parent !== undefined && p.parent !== '')
				.map((p) => [p.parent, p.category]);
			if (edges.some(([p]) => !names.includes(p)))
				fail('UnknownParentError', path + '.data', 'unknown tree parent');
			s.nodeOrder = topological(names, edges, path + '.data');
		}
		if (s.type === 'sankey')
			s.nodeOrder = topological(
				uniq(s.points.flatMap((p) => [p.source, p.target])),
				s.points.map((p) => [p.source, p.target]),
				path + '.data',
			);
		if (s.type === 'waterfall') {
			let sum = 0;
			for (const [i, p] of s.points.entries()) {
				const value = p[s.horizontal ? 'x' : 'y'];
				if (value === null) {
					p.start = null;
					p.end = null;
					continue;
				}
				if (p.isTotal) {
					if (i > 0 && Math.abs(value - sum) > 1e-8 * Math.max(1, Math.abs(sum)))
						warnings.push({
							code: 'WaterfallTotalMismatchWarning',
							path: `${path}.data.rows[${p.row}]`,
							expected: sum,
							actual: value,
						});
					p.start = 0;
					p.end = value;
					sum = value;
				} else {
					p.start = sum;
					sum += value;
					p.end = sum;
				}
			}
		}
	}
	for (const side of ['x', 'y']) {
		const axes = element[side + 'Axis'];
		for (const [i, a] of (Array.isArray(axes) ? axes : [axes || {}]).entries()) {
			checkRange(a.label?.rotation, -90, 90, `${path}.${side}Axis[${i}].label.rotation`);
			if (
				a.label?.interval !== undefined &&
				(!Number.isInteger(a.label.interval) ||
					a.label.interval < 0 ||
					a.label.interval > 100)
			)
				fail(
					'InvalidValueError',
					`${path}.${side}Axis[${i}].label.interval`,
					'expected integer [0,100]',
				);
			if (
				a.label?.numberFormat &&
				!/^(0(?:\.0+)?%?|#,##0|0\.0E\+00)$/.test(a.label.numberFormat)
			)
				fail(
					'InvalidValueError',
					`${path}.${side}Axis[${i}].label.numberFormat`,
					'unsupported number format',
				);
			checkEnum(a.type, ['category', 'value'], `${path}.${side}Axis[${i}].type`);
			for (const k of ['min', 'max'])
				if (a[k] !== undefined) numeric(a[k], `${path}.${side}Axis[${i}].${k}`);
			if (a.min !== undefined && a.max !== undefined && a.min >= a.max)
				fail('InvalidValueError', `${path}.${side}Axis[${i}]`, 'min must be less than max');
		}
		for (const s of series)
			if ((s[side + 'AxisIndex'] || 0) > 0 && s[side + 'Type'] !== 'value')
				fail('AxisIndexError', path + '.series', 'secondary axes must be value axes');
	}
	return { series, warnings, types, nullHandling };
}

function resolveColor(value, theme = {}) {
	if (typeof value !== 'string') return value;
	const seen = new Set();
	while (value.startsWith('$')) {
		if (seen.has(value)) fail('CyclicColorError', value, 'cyclic theme color reference');
		seen.add(value);
		const color = theme.colors?.[value.slice(1)];
		if (!color) fail('UnknownColorError', value, 'theme color is undefined');
		value = color;
	}
	return value;
}
function paint(value, theme) {
	if (typeof value === 'string') return resolveColor(value, theme);
	if (!value) return undefined;
	if (['linear', 'radial'].includes(value.type) && Array.isArray(value.colorStops)) return value;
	if (value.type === 'solid') return resolveColor(value.color, theme);
	if (value.type !== 'gradient' || !Array.isArray(value.stops) || value.stops.length < 2)
		fail('InvalidValueError', 'fill', 'expected color or gradient with at least two stops');
	for (const stop of value.stops) checkRange(stop.position, 0, 1, 'fill.stops.position');
	const colorStops = value.stops.map((s) => ({
		offset: s.position,
		color: resolveColor(s.color, theme),
	}));
	if (value.gradientType === 'radial')
		return { type: 'radial', x: 0.5, y: 0.5, r: 0.5, colorStops };
	const a = ((value.angle || 0) * Math.PI) / 180,
		dx = Math.cos(a) / 2,
		dy = Math.sin(a) / 2;
	return { type: 'linear', x: 0.5 - dx, y: 0.5 - dy, x2: 0.5 + dx, y2: 0.5 + dy, colorStops };
}
function fontFamily(value) {
	return typeof value === 'string' ? value : value ? `${value.latin}, ${value.ea}` : 'MiSans';
}
function textStyle(value, chart, theme) {
	return {
		color: resolveColor(value?.color || theme.colors?.text || '#000000', theme),
		fontSize: value?.fontSize || Math.max(10, Math.min(16, (chart.bounds?.[3] || 360) / 30)),
		fontFamily: fontFamily(
			value?.fontFamily ||
				chart.fontFamily ||
				theme.fontFamily ||
				theme.textStyles?.body?.fontFamily,
		),
	};
}
function lineStyle(value, theme, fallback) {
	const c = config(value);
	return {
		color: resolveColor(c.color || fallback || '#cccccc', theme),
		width: c.width ?? 1,
		type: { dash: 'dashed', dot: 'dotted' }[c.style] || 'solid',
	};
}
function border(value, theme) {
	return value
		? {
				borderColor: resolveColor(value.color || '#000000', theme),
				borderWidth: value.width ?? 1,
				borderType: { dash: 'dashed', dot: 'dotted' }[value.style] || 'solid',
			}
		: { borderWidth: 0 };
}
export function formatChartNumber(value, format) {
	if (value === null || value === undefined || Number.isNaN(value)) return '';
	if (!format) return String(value);
	if (format === '#,##0')
		return Number(value).toLocaleString('en-US', { maximumFractionDigits: 0 });
	if (format === '0.0E+00')
		return Number(value)
			.toExponential(1)
			.replace(/e([+-])(\d+)$/, (_, s, e) => `E${s}${e.padStart(2, '0')}`);
	const decimals = (format.match(/\.(0+)/)?.[1] || '').length;
	return (
		(Number(value) * (format.endsWith('%') ? 100 : 1)).toFixed(decimals) +
		(format.endsWith('%') ? '%' : '')
	);
}
function label(s, chart, theme) {
	const c = s.dataLabels,
		content = c.content || (['treemap', 'sunburst'].includes(s.type) ? 'category' : 'value');
	return {
		show: c.show ?? false,
		...(c.position
			? {
					position:
						c.position === 'outsideEnd' ? (s.horizontal ? 'right' : 'top') : c.position,
				}
			: {}),
		...textStyle(c, chart, theme),
		formatter: (p) => {
			if (content === 'category') return p.name || p.data?.category || '';
			if (content === 'percentage')
				return formatChartNumber((p.percent || 0) / 100, c.numberFormat || '0%');
			const v =
				p.data?.rawValue ??
				(Array.isArray(p.value)
					? p.value[s.horizontal ? 0 : s.type === 'heatmap' ? 2 : 1]
					: p.value);
			return formatChartNumber(v, c.numberFormat);
		},
	};
}
function darken(color, depth) {
	if (!depth) return color;
	if (isObject(color))
		return {
			...color,
			colorStops: color.colorStops.map((s) => ({ ...s, color: darken(s.color, depth) })),
		};
	const rgba = echarts.color.parse(color);
	if (!rgba) return color;
	const [r, g, b, a] = rgba,
		hsl = echarts.color.modifyHSL(
			`rgb(${r},${g},${b})`,
			null,
			null,
			Math.max(0, rgbLightness(r, g, b) - 0.1 * depth),
		);
	return a < 1
		? echarts.color.modifyAlpha(hsl, a)
		: echarts.color.toHex(hsl)
			? '#' + echarts.color.toHex(hsl)
			: hsl;
}
function rgbLightness(r, g, b) {
	return (Math.max(r, g, b) + Math.min(r, g, b)) / 510;
}
function treeData(s, theme) {
	const roots = s.points.filter((p) => !p.parent),
		table = new Map(s.points.map((p) => [p.category, p]));
	function walk(p, rootIndex, depth) {
		let fills = s.fill ?? CHART_PALETTE,
			rootPaint = Array.isArray(fills) ? fills[rootIndex % fills.length] : fills,
			color;
		if (Array.isArray(rootPaint)) {
			const at = Math.min(depth, rootPaint.length - 1);
			color = darken(paint(rootPaint[at], theme), depth - at);
		} else
			color =
				s.type === 'treemap'
					? darken(paint(rootPaint, theme), depth)
					: paint(rootPaint, theme);
		const children = [...table.values()].filter((n) => n.parent === p.category);
		return {
			name: p.category,
			value: p.value,
			rawValue: p.value,
			itemStyle: { color, ...border(s.border, theme) },
			...(children.length && (!s.levels || depth + 1 < s.levels)
				? { children: children.map((c) => walk(c, rootIndex, depth + 1)) }
				: {}),
		};
	}
	return roots.map((p, i) => walk(p, i, 0));
}
function positionLegend(c, fallback) {
	const pos = c.position || fallback;
	return {
		orient: ['left', 'right'].includes(pos) ? 'vertical' : 'horizontal',
		...(pos === 'top'
			? { top: 5, left: 'center' }
			: pos === 'bottom'
				? { bottom: 5, left: 'center' }
				: { [pos]: 5, top: 'middle' }),
	};
}
function customBar(s, chart, theme, color, kind) {
	const horizontal = s.horizontal;
	return (params, api) => {
		const values = params.dataIndex,
			point = s.points[values],
			category = api.value(0),
			lo = api.value(1),
			hi = api.value(2);
		if (lo === null || hi === null) return;
		const a = api.coord(horizontal ? [lo, category] : [category, lo]),
			b = api.coord(horizontal ? [hi, category] : [category, hi]);
		const slot = api.size(horizontal ? [0, 1] : [1, 0]),
			thick =
				Math.abs(slot[horizontal ? 1 : 0]) *
				(chart.barWidth ?? 1 - (chart.categoryGap ?? 0.2)) *
				0.8;
		const children = [];
		if (kind === 'waterfall') {
			const key = point.isTotal
					? 'totalBars'
					: point.end >= point.start
						? 'increaseBars'
						: 'decreaseBars',
				style = s[key] || {},
				fill = paint(
					style.fill ||
						{
							totalBars: CHART_PALETTE[0],
							increaseBars: '#67B8A6',
							decreaseBars: '#D96060',
						}[key],
					theme,
				);
			children.push({
				type: 'rect',
				shape: {
					x: horizontal ? Math.min(a[0], b[0]) : a[0] - thick / 2,
					y: horizontal ? a[1] - thick / 2 : Math.min(a[1], b[1]),
					width: horizontal ? Math.abs(b[0] - a[0]) : thick,
					height: horizontal ? thick : Math.abs(b[1] - a[1]),
				},
				style: {
					fill,
					stroke: style.border
						? resolveColor(style.border.color || '#000000', theme)
						: undefined,
					lineWidth: style.border?.width ?? 1,
				},
			});
		} else {
			const wick = s.wickStyle || {},
				close = api.value(3),
				cp = api.coord([category, close]);
			children.push({
				type: 'line',
				shape: { x1: a[0], y1: a[1], x2: b[0], y2: b[1] },
				style: {
					stroke: resolveColor(wick.color || color, theme),
					lineWidth: wick.width ?? 1,
					lineDash:
						wick.style === 'dash' ? [5, 3] : wick.style === 'dot' ? [1, 2] : undefined,
				},
			});
			children.push({
				type: 'circle',
				shape: { cx: cp[0], cy: cp[1], r: 3 },
				style: { fill: color },
			});
		}
		if (kind === 'waterfall' && s.dataLabels.show)
			children.push({
				type: 'text',
				style: {
					x: b[0],
					y: b[1] - 8,
					text:
						s.dataLabels.content === 'category'
							? String(point[horizontal ? 'y' : 'x'])
							: formatChartNumber(
									point[horizontal ? 'x' : 'y'],
									s.dataLabels.numberFormat,
								),
					fill: textStyle(s.dataLabels, chart, theme).color,
					font: `${textStyle(s.dataLabels, chart, theme).fontSize}px ${textStyle(s.dataLabels, chart, theme).fontFamily}`,
					align: 'center',
					verticalAlign: 'bottom',
				},
			});
		return { type: 'group', children };
	};
}

function pictographicSymbol(symbol) {
	if (typeof symbol === 'string') symbol = { shapeName: symbol };
	const name = symbol.shapeName,
		adj = (symbol.adjustments || []).map((v) => v / 1000);
	if (name === 'custom') {
		if (!symbol.path || !symbol.viewBox?.every((v) => v > 0))
			fail('InvalidValueError', 'symbol', 'custom ShapeDef requires path and viewBox');
		return 'path://' + symbol.path;
	}
	if (['rect', 'ellipse', 'diamond', 'roundRect'].includes(name) && !adj.length)
		return { ellipse: 'circle' }[name] || name;
	const paths = {
		triangle: `M0 100L${adj[0] ?? 50} 0L100 100Z`,
		rtTriangle: 'M0 0L100 100L0 100Z',
		parallelogram: `M${adj[0] ?? 25} 0L100 0L${100 - (adj[0] ?? 25)} 100L0 100Z`,
		trapezoid: `M${adj[0] ?? 25} 0L${100 - (adj[0] ?? 25)} 0L100 100L0 100Z`,
		hexagon: `M${adj[0] ?? 25} 0L${100 - (adj[0] ?? 25)} 0L100 50L${100 - (adj[0] ?? 25)} 100L${adj[0] ?? 25} 100L0 50Z`,
		heart: 'M50 100C0 65 -20 0 20 0C40 0 50 15 50 25C50 15 60 0 80 0C120 0 100 65 50 100Z',
		roundRect: `M${adj[0] ?? 16.667} 0H${100 - (adj[0] ?? 16.667)}Q100 0 100 ${adj[0] ?? 16.667}V${100 - (adj[0] ?? 16.667)}Q100 100 ${100 - (adj[0] ?? 16.667)} 100H${adj[0] ?? 16.667}Q0 100 0 ${100 - (adj[0] ?? 16.667)}V${adj[0] ?? 16.667}Q0 0 ${adj[0] ?? 16.667} 0Z`,
	};
	if (paths[name]) return 'path://' + paths[name];
	const sides = { pentagon: 5, heptagon: 7, octagon: 8, decagon: 10, dodecagon: 12 }[name],
		star = /^star(\d+)$/.exec(name || '');
	if (sides || star) {
		const n = sides || Number(star[1]) * 2;
		return (
			'path://' +
			Array.from({ length: n }, (_, i) => {
				const a = -Math.PI / 2 + (2 * Math.PI * i) / n,
					r = star && i % 2 ? (adj[0] ?? 25) : 50;
				return `${i ? 'L' : 'M'}${50 + r * Math.cos(a)} ${50 + r * Math.sin(a)}`;
			}).join('') +
			'Z'
		);
	}
	fail(
		'UnsupportedShapeError',
		'symbol.shapeName',
		`pictographic shape ${name} requires a custom SVG path`,
	);
}
function radarRenderer(s, chart, theme, radar, color) {
	return (_params, api) => {
		const w = api.getWidth(),
			h = api.getHeight(),
			cx = w / 2,
			cy = h * (chart.title ? 0.54 : 0.5),
			radius = ((Math.min(w, h) / 2) * Number.parseFloat(radar.radius)) / 100;
		const min = radar.indicator[0]?.min ?? 0,
			max = radar.indicator[0]?.max ?? 1,
			n = s.points.length;
		const points = s.points.map((p, i) => {
			const angle = -Math.PI / 2 - (2 * Math.PI * i) / n,
				r = (radius * ((p.y ?? min) - min)) / (max - min);
			return p.y === null ? null : [cx + Math.cos(angle) * r, cy + Math.sin(angle) * r];
		});
		const marker = config(s.marker),
			children = [],
			line = {
				stroke: paint(s.lineColor || color, theme),
				lineWidth: s.width ?? 2,
				lineDash:
					s.lineStyle === 'dash' ? [5, 3] : s.lineStyle === 'dot' ? [1, 2] : undefined,
			};
		let segments;
		if (s.nullHandling === 'connect') segments = [points.filter(Boolean)];
		else if (points.every(Boolean)) segments = [points];
		else {
			const gap = points.indexOf(null),
				ordered = [...points.slice(gap + 1), ...points.slice(0, gap + 1)];
			segments = [[]];
			for (const p of ordered) {
				if (p) segments.at(-1).push(p);
				else if (segments.at(-1).length) segments.push([]);
			}
		}
		for (const segment of segments.filter((a) => a.length > 1)) {
			const closed = s.nullHandling === 'connect' || points.every(Boolean);
			children.push({
				type: closed ? 'polygon' : 'polyline',
				shape: { points: segment, smooth: s.smooth ? 0.2 : 0 },
				style: {
					...line,
					fill: closed ? paint(s.areaColor || s.lineColor || color, theme) : 'none',
					opacity: s.areaColor ? 1 : 0.65,
				},
			});
		}
		for (const [i, p] of points.entries())
			if (p) {
				if (s.marker) {
					const size = marker.size || 7,
						shape =
							marker.shape === 'diamond'
								? {
										points: [
											[p[0], p[1] - size / 2],
											[p[0] + size / 2, p[1]],
											[p[0], p[1] + size / 2],
											[p[0] - size / 2, p[1]],
										],
									}
								: marker.shape === 'triangle'
									? {
											points: [
												[p[0], p[1] - size / 2],
												[p[0] + size / 2, p[1] + size / 2],
												[p[0] - size / 2, p[1] + size / 2],
											],
										}
									: marker.shape === 'rect'
										? {
												x: p[0] - size / 2,
												y: p[1] - size / 2,
												width: size,
												height: size,
											}
										: { cx: p[0], cy: p[1], r: size / 2 };
					children.push({
						type:
							marker.shape === 'diamond' || marker.shape === 'triangle'
								? 'polygon'
								: marker.shape === 'rect'
									? 'rect'
									: 'circle',
						shape,
						style: {
							fill: paint(marker.fill || color, theme),
							stroke: marker.border
								? resolveColor(marker.border.color || '#000000', theme)
								: undefined,
							lineWidth: marker.border?.width ?? 0,
						},
					});
				}
				if (s.dataLabels.show) {
					const style = textStyle(s.dataLabels, chart, theme);
					children.push({
						type: 'text',
						style: {
							x: p[0],
							y: p[1] - 8,
							text: formatChartNumber(s.points[i].y, s.dataLabels.numberFormat),
							fill: style.color,
							font: `${style.fontSize}px ${style.fontFamily}`,
							align: 'center',
						},
					});
				}
			}
		return { type: 'group', children };
	};
}

export function buildChartOption(chart, theme = {}) {
	const normalized = validateChart(chart),
		series = normalized.series,
		types = normalized.types;
	const option = {
		animation: false,
		color: CHART_PALETTE,
		backgroundColor:
			chart.fill && chart.fill.type !== 'image' ? paint(chart.fill, theme) : 'transparent',
		textStyle: textStyle({}, chart, theme),
		tooltip: { trigger: 'item' },
		series: [],
	};
	if (chart.title) {
		const c = typeof chart.title === 'string' ? { text: chart.title } : chart.title;
		option.title = { text: c.text, left: 'center', textStyle: textStyle(c, chart, theme) };
	}
	const legendApplicable = types.some(
		(t) => !['treemap', 'sunburst', 'sankey', 'heatmap'].includes(t),
	);
	option.legend = {
		show: legendApplicable && enabled(chart.legend, !types.includes('waterfall')),
		...positionLegend(config(chart.legend), 'bottom'),
		textStyle: textStyle(config(chart.legend), chart, theme),
	};
	if (types.some((t) => XY.includes(t))) {
		option.grid = {
			left: 55,
			right: Array.isArray(chart.yAxis) && chart.yAxis.length > 1 ? 60 : 30,
			top: chart.title ? 50 : 25,
			bottom: option.legend.show ? 60 : 35,
			containLabel: true,
		};
		for (const side of ['x', 'y']) {
			const source = chart[side + 'Axis'],
				axes = Array.isArray(source) ? source : [source || {}];
			option[side + 'Axis'] = axes.map((a, i) => {
				const refs = series.filter(
						(s) => XY.includes(s.type) && (s[side + 'AxisIndex'] || 0) === i,
					),
					inferred = refs[0]?.[side + 'Type'] || 'value',
					type = a.type || inferred,
					labels = config(a.label),
					axisLine = config(a.axisLine),
					arrow = axisLine.arrow;
				const values = uniq(
					refs
						.flatMap((s) => s.points.map((p) => p[side]))
						.filter((v) => v !== null && v !== undefined),
				);
				const title = typeof a.title === 'string' ? { text: a.title } : a.title;
				return {
					show: a.show ?? true,
					type,
					data: type === 'category' ? values : undefined,
					min: a.min,
					max: a.max,
					inverse: a.reverse ?? false,
					position: side === 'y' ? (i ? 'right' : 'left') : i ? 'top' : 'bottom',
					offset: Math.max(0, i - 1) * 50,
					name: title?.text,
					nameTextStyle: textStyle(title, chart, theme),
					axisLabel: {
						show: enabled(a.label),
						rotate: labels.rotation,
						interval: labels.interval,
						...textStyle(labels, chart, theme),
						...(labels.numberFormat && type === 'value'
							? { formatter: (v) => formatChartNumber(v, labels.numberFormat) }
							: {}),
					},
					axisLine: {
						show: enabled(a.axisLine),
						lineStyle: lineStyle(a.axisLine, theme, '#888888'),
						symbol:
							arrow === 'both'
								? ['arrow', 'arrow']
								: arrow === 'start'
									? ['arrow', 'none']
									: arrow
										? ['none', 'arrow']
										: ['none', 'none'],
					},
					splitLine: {
						show: enabled(a.gridLine),
						lineStyle: lineStyle(a.gridLine, theme),
					},
				};
			});
		}
	}
	if (types.includes('radar')) {
		const a = chart.spokeAxis || {},
			values = series.flatMap((s) => s.points.map((p) => p.y).filter((v) => v !== null));
		option.radar = {
			center: ['50%', chart.title ? '54%' : '50%'],
			radius: option.legend.show ? '63%' : '72%',
			indicator: series[0].points.map((p) => ({
				name: String(p.category),
				min: a.min ?? 0,
				max: a.max ?? Math.max(1, ...values),
			})),
			axisName: {
				show: enabled(a.label) && enabled(a.show),
				...textStyle(config(a.label), chart, theme),
			},
			axisLine: {
				show: enabled(a.axisLine) && enabled(a.show),
				lineStyle: lineStyle(a.axisLine, theme),
			},
			splitLine: {
				show: enabled(a.gridLine) && enabled(a.show),
				lineStyle: lineStyle(a.gridLine, theme),
			},
			splitArea: { show: false },
		};
		if (enabled(a.show) && enabled(a.label)) {
			const width = chart.bounds?.[2] || 640,
				height = chart.bounds?.[3] || 360,
				radius =
					((Math.min(width, height) / 2) * Number.parseFloat(option.radar.radius)) / 100;
			const lo = a.min ?? 0,
				hi = a.max ?? Math.max(1, ...values),
				style = textStyle(config(a.label), chart, theme);
			option.graphic = Array.from({ length: 6 }, (_, i) => ({
				type: 'text',
				silent: true,
				z: 20,
				style: {
					x: width / 2 + 7,
					y: height * (chart.title ? 0.54 : 0.5) - (radius * i) / 5,
					text: formatChartNumber(lo + ((hi - lo) * i) / 5, config(a.label).numberFormat),
					fill: style.color,
					font: `${style.fontSize}px ${style.fontFamily}`,
					align: 'left',
					verticalAlign: 'middle',
				},
			}));
		}
	}
	const byType = {};
	for (const [i, s] of series.entries()) {
		const colorIndex = byType[s.type] || 0;
		byType[s.type] = colorIndex + 1;
		const color = paint(
				(typeof s.fill === 'string' || s.fill?.type ? s.fill : null) ||
					s.lineColor ||
					CHART_PALETTE[colorIndex % 10],
				theme,
			),
			marker = config(s.marker),
			body = {
				name: s.name,
				label: label(s, chart, theme),
				emphasis: { disabled: true },
				xAxisIndex: s.xAxisIndex || 0,
				yAxisIndex: s.yAxisIndex || 0,
			};
		const category = s.horizontal ? 'y' : 'x',
			value = s.horizontal ? 'x' : 'y';
		const pointData = s.points.map((p) => ({
			value: [p.x, p.y],
			name: String(p[category] ?? ''),
			rawValue: p[value],
		}));
		if (['bar', 'line', 'area'].includes(s.type)) {
			Object.assign(body, {
				type: s.type === 'bar' ? (s.symbol ? 'pictorialBar' : 'bar') : 'line',
				data: pointData,
				itemStyle: { color, ...border(s.border, theme) },
				lineStyle: {
					color: paint(s.lineColor || color, theme),
					width: s.width ?? 2,
					type: { dash: 'dashed', dot: 'dotted' }[s.lineStyle] || 'solid',
				},
				smooth: s.smooth ?? false,
				showSymbol: s.marker !== false && !!s.marker,
				symbol: marker.shape || 'circle',
				symbolSize: marker.size || 7,
				connectNulls: s.nullHandling === 'connect',
				barWidth: chart.barWidth ? `${chart.barWidth * 100}%` : undefined,
				barGap: `${(chart.barGap || 0) * 100}%`,
				barCategoryGap: `${(chart.categoryGap ?? 0.2) * 100}%`,
			});
			if (s.marker)
				body.itemStyle = {
					color: paint(marker.fill || color, theme),
					...border(marker.border || s.border, theme),
				};
			if (s.symbol) body.symbol = pictographicSymbol(s.symbol);
			if (s.type === 'area')
				body.areaStyle = {
					color: paint(s.areaColor || s.lineColor || color, theme),
					opacity: s.areaColor ? 1 : 0.45,
				};
			if (s.stack) {
				body.stack = s.type;
				body.stackStrategy = 'all';
				if (s.stack === 'percent')
					for (const p of body.data) {
						const v = p.value[s.horizontal ? 0 : 1];
						const total = series
							.filter((other) => other.type === s.type && other.stack)
							.reduce(
								(n, other) =>
									n +
									(other.points.find(
										(q) => q[category] === p.value[s.horizontal ? 1 : 0],
									)?.[value] || 0),
								0,
							);
						p.value[s.horizontal ? 0 : 1] =
							v === null ? null : total === 0 ? 0 : v / total;
					}
				if (s.stack === 'stream') {
					body.type = 'custom';
					delete body.stack;
					body.data = s.points.map((p) => {
						const group = series.filter(
								(other) => other.type === 'area' && other.stack === 'stream',
							),
							v = p[value];
						const at = group.indexOf(s);
						const vals = group.map(
							(other) =>
								other.points.find((q) => q[category] === p[category])?.[value] ?? 0,
						);
						const start =
							-vals.reduce((a, b) => a + b, 0) / 2 +
							vals.slice(0, at).reduce((a, b) => a + b, 0);
						return {
							value: [
								p[category],
								v === null ? null : start,
								v === null ? null : start + v,
							],
							rawValue: v,
						};
					});
					body.encode = s.horizontal ? { x: [1, 2], y: 0 } : { x: 0, y: [1, 2] };
					body.renderItem = (params, api) => {
						if (params.dataIndex !== 0) return;
						const segments = [[]];
						for (const p of body.data) {
							if (p.value[1] === null) {
								if (s.nullHandling === 'gap' && segments.at(-1).length)
									segments.push([]);
								continue;
							}
							segments.at(-1).push(p.value);
						}
						const result = {
							type: 'group',
							children: segments
								.filter((a) => a.length > 1)
								.map((segment) => ({
									type: 'polygon',
									shape: {
										points: [
											...segment.map((p) =>
												api.coord(
													s.horizontal ? [p[1], p[0]] : [p[0], p[1]],
												),
											),
											...segment
												.slice()
												.reverse()
												.map((p) =>
													api.coord(
														s.horizontal ? [p[2], p[0]] : [p[0], p[2]],
													),
												),
										],
										smooth: s.smooth ? 0.2 : 0,
									},
									style: {
										fill: paint(s.areaColor || color, theme),
										stroke: paint(s.lineColor || color, theme),
										lineWidth: s.width ?? 2,
										lineDash:
											s.lineStyle === 'dash'
												? [5, 3]
												: s.lineStyle === 'dot'
													? [1, 2]
													: undefined,
										opacity: s.areaColor ? 1 : 0.45,
									},
								})),
						};
						for (const p of body.data)
							if (p.value[1] !== null) {
								const at = api.coord(
									s.horizontal
										? [p.value[2], p.value[0]]
										: [p.value[0], p.value[2]],
								);
								const m = config(s.marker),
									size = m.size || 7;
								if (s.marker) {
									const shape =
										m.shape === 'rect'
											? {
													x: at[0] - size / 2,
													y: at[1] - size / 2,
													width: size,
													height: size,
												}
											: m.shape === 'diamond'
												? {
														points: [
															[at[0], at[1] - size / 2],
															[at[0] + size / 2, at[1]],
															[at[0], at[1] + size / 2],
															[at[0] - size / 2, at[1]],
														],
													}
												: m.shape === 'triangle'
													? {
															points: [
																[at[0], at[1] - size / 2],
																[
																	at[0] + size / 2,
																	at[1] + size / 2,
																],
																[
																	at[0] - size / 2,
																	at[1] + size / 2,
																],
															],
														}
													: { cx: at[0], cy: at[1], r: size / 2 };
									result.children.push({
										type:
											m.shape === 'rect'
												? 'rect'
												: ['diamond', 'triangle'].includes(m.shape)
													? 'polygon'
													: 'circle',
										shape,
										style: {
											fill: paint(m.fill || color, theme),
											stroke: m.border
												? resolveColor(m.border.color || '#000000', theme)
												: undefined,
											lineWidth: m.border?.width ?? 0,
										},
									});
								}
								if (s.dataLabels.show) {
									const style = textStyle(s.dataLabels, chart, theme),
										middle = (p.value[1] + p.value[2]) / 2,
										xy = api.coord(
											s.horizontal
												? [middle, p.value[0]]
												: [p.value[0], middle],
										);
									result.children.push({
										type: 'text',
										style: {
											x: xy[0],
											y: xy[1],
											text: formatChartNumber(
												p.rawValue,
												s.dataLabels.numberFormat,
											),
											fill: style.color,
											font: `${style.fontSize}px ${style.fontFamily}`,
											align: 'center',
											verticalAlign: 'middle',
										},
									});
								}
							}
						return result;
					};
				}
			}
		} else if (['scatter', 'bubble'].includes(s.type)) {
			Object.assign(body, {
				type: 'scatter',
				data: s.points
					.filter(
						(p) =>
							p.x !== null &&
							p.y !== null &&
							(s.type !== 'bubble' || p.size !== null),
					)
					.map((p) => ({
						value: s.type === 'bubble' ? [p.x, p.y, p.size] : [p.x, p.y],
						rawValue: p.y,
					})),
				symbol: marker.shape || 'circle',
				symbolSize: marker.size || 9,
				itemStyle: {
					color: paint(marker.fill || color, theme),
					...border(marker.border || s.border, theme),
				},
			});
			if (s.type === 'bubble') {
				const transform = (v) =>
						s.sizeScale === 'linear'
							? v
							: s.sizeScale === 'log'
								? Math.log1p(v)
								: Math.sqrt(v),
					sizes = s.points.filter((p) => p.size !== null).map((p) => transform(p.size)),
					max = Math.max(0, ...sizes),
					min = Math.min(0, ...sizes),
					[lo, hi] = s.sizeRange || [0, 24];
				body.symbolSize = (values) =>
					values[2] <= 0
						? 0
						: 2 *
							(lo +
								(max === min ? 1 : (transform(values[2]) - min) / (max - min)) *
									(hi - lo));
			}
		} else if (s.type === 'candlestick') {
			if (s.encode.open) {
				Object.assign(body, {
					type: 'custom',
					encode: { x: 0, y: [1, 2, 3, 4] },
					data: s.points.map((p) => ({ value: [p.x, p.open, p.close, p.low, p.high] })),
					renderItem: (params, api) => {
						const p = s.points[params.dataIndex];
						if (['open', 'close', 'low', 'high'].some((k) => p[k] === null)) return;
						const at = api.value(0),
							low = api.coord([at, p.low]),
							high = api.coord([at, p.high]),
							open = api.coord([at, p.open]),
							close = api.coord([at, p.close]);
						const width =
							Math.abs(api.size([1, 0])[0]) * (1 - (chart.categoryGap ?? 0.2)) * 0.65;
						const bars = p.close > p.open ? s.upBars || {} : s.downBars || {},
							fill = paint(
								bars.fill || (p.close > p.open ? '#67B8A6' : '#D96060'),
								theme,
							),
							wick = s.wickStyle || {};
						return {
							type: 'group',
							children: [
								{
									type: 'line',
									shape: { x1: low[0], y1: low[1], x2: high[0], y2: high[1] },
									style: {
										stroke: resolveColor(wick.color || '#333333', theme),
										lineWidth: wick.width ?? 1,
										lineDash:
											wick.style === 'dash'
												? [5, 3]
												: wick.style === 'dot'
													? [1, 2]
													: undefined,
									},
								},
								{
									type: 'rect',
									shape: {
										x: open[0] - width / 2,
										y: Math.min(open[1], close[1]),
										width,
										height: Math.max(1, Math.abs(open[1] - close[1])),
									},
									style: {
										fill,
										stroke: bars.border
											? resolveColor(bars.border.color || '#000000', theme)
											: undefined,
										lineWidth: bars.border?.width ?? 0,
										lineDash:
											bars.border?.style === 'dash'
												? [5, 3]
												: bars.border?.style === 'dot'
													? [1, 2]
													: undefined,
									},
								},
							],
						};
					},
				});
			} else
				Object.assign(body, {
					type: 'custom',
					encode: { x: 0, y: [1, 2, 3] },
					data: s.points.map((p) => ({ value: [p.x, p.low, p.high, p.close] })),
					renderItem: customBar(s, chart, theme, color, 'hlc'),
				});
		} else if (s.type === 'pie') {
			const fills = Array.isArray(s.fill) ? s.fill : null;
			Object.assign(body, {
				type: 'pie',
				clockwise: true,
				startAngle: 90 - (s.startAngle || 0),
				radius: [`${(s.innerRadius || 0) * 75}%`, '75%'],
				center: ['50%', chart.title ? '54%' : '50%'],
				data: s.points
					.filter((p) => p.value !== null)
					.map((p, j) => ({
						value: p.value,
						name: String(p.category ?? ''),
						itemStyle: {
							color: paint(
								fills ? fills[j % fills.length] : s.fill || CHART_PALETTE[j % 10],
								theme,
							),
							...border(s.border, theme),
						},
					})),
			});
		} else if (s.type === 'radar') {
			Object.assign(body, {
				type: 'radar',
				data: [{ name: s.name, value: s.points.map((p) => p.y) }],
				symbol: s.marker ? marker.shape || 'circle' : 'none',
				symbolSize: marker.size || 7,
				lineStyle: {
					color: paint(s.lineColor || color, theme),
					width: s.width ?? 2,
					type: { dash: 'dashed', dot: 'dotted' }[s.lineStyle] || 'solid',
				},
				itemStyle: {
					color: paint(marker.fill || color, theme),
					...border(marker.border, theme),
				},
				areaStyle: {
					color: paint(s.areaColor || s.lineColor || color, theme),
					opacity: s.areaColor ? 1 : 0.45,
				},
			});
			if (s.smooth || s.points.some((p) => p.y === null))
				Object.assign(body, {
					type: 'custom',
					coordinateSystem: 'none',
					data: [0],
					renderItem: radarRenderer(s, chart, theme, option.radar, color),
				});
		} else if (s.type === 'waterfall')
			Object.assign(body, {
				type: 'custom',
				encode: s.horizontal ? { x: [1, 2], y: 0 } : { x: 0, y: [1, 2] },
				data: s.points.map((p) => ({
					value: [p[category], p.start, p.end],
					rawValue: p[value],
				})),
				renderItem: customBar(s, chart, theme, color, 'waterfall'),
			});
		else if (s.type === 'heatmap') {
			const xs = uniq(s.points.map((p) => p.x)),
				ys = uniq(s.points.map((p) => p.y)),
				values = s.points.map((p) => p.value).filter((v) => v !== null),
				scale = s.colorScale || {},
				diverging = scale.type === 'diverging',
				max = Math.max(1, ...values.map(Math.abs));
			checkEnum(scale.type, ['linear', 'diverging'], 'colorScale.type');
			const colors =
				s.colorScheme ||
				(diverging
					? ['#D96060', '#ffffff', CHART_PALETTE[0]]
					: ['#ffffff', CHART_PALETTE[0]]);
			if (colors.length < 2 || (diverging && colors.length !== 3))
				fail(
					'InvalidValueError',
					'colorScheme',
					'diverging requires 3 colors, linear requires at least 2',
				);
			const domain =
				scale.domain ||
				(diverging
					? [-max, max]
					: [
							values.length ? Math.min(...values) : 0,
							values.length ? Math.max(...values) : 1,
						]);
			if (!scale.domain && domain[0] === domain[1]) domain[1] = domain[0] + 1;
			if (
				!Array.isArray(domain) ||
				domain.length !== 2 ||
				!domain.every((v) => typeof v === 'number' && Number.isFinite(v)) ||
				domain[0] >= domain[1]
			)
				fail('InvalidValueError', 'colorScale.domain', 'expected increasing pair');
			option.visualMap = {
				show: enabled(s.colorbar),
				min: domain[0],
				max: domain[1],
				dimension: 2,
				calculable: false,
				...positionLegend(config(s.colorbar), 'right'),
				inRange: { color: colors.map((c) => resolveColor(c, theme)) },
				textStyle: textStyle(config(s.colorbar), chart, theme),
			};
			option.legend.show = false;
			Object.assign(body, {
				type: 'heatmap',
				data: s.points
					.filter((p) => p.value !== null)
					.map((p) => ({
						value: [xs.indexOf(p.x), ys.indexOf(p.y), p.value],
						rawValue: p.value,
					})),
			});
		} else if (['treemap', 'sunburst'].includes(s.type)) {
			Object.assign(body, {
				type: s.type,
				data: treeData(s, theme),
				roam: false,
				nodeClick: false,
				sort: null,
				...(s.type === 'treemap'
					? {
							breadcrumb: { show: false },
							levels: [{ label: { show: false }, upperLabel: { show: false } }],
							leafDepth: s.levels,
							upperLabel: {
								show: s.dataLabels.show ?? false,
								...textStyle(s.dataLabels, chart, theme),
							},
						}
					: { radius: [0, '78%'], emphasis: { disabled: true } }),
			});
		} else if (s.type === 'sankey') {
			const fills = s.fill;
			Object.assign(body, {
				type: 'sankey',
				nodeAlign: s.nodeAlign || 'justify',
				draggable: false,
				left: 30,
				right: 60,
				top: chart.title ? 50 : 25,
				bottom: 25,
				data: s.nodeOrder.map((name, j) => ({
					name,
					itemStyle: {
						color: paint(
							Array.isArray(fills)
								? fills[j % fills.length]
								: isObject(fills) && !fills.type
									? fills[name] || CHART_PALETTE[j % 10]
									: fills || CHART_PALETTE[j % 10],
							theme,
						),
						...border(s.border, theme),
					},
				})),
				links: s.points
					.filter((p) => p.flow !== null)
					.map((p) => ({ source: p.source, target: p.target, value: p.flow })),
				lineStyle: { color: 'source', opacity: 0.35, curveness: 0.5 },
			});
		}
		option.series.push(body);
	}
	return option;
}

function escapeXml(value) {
	return String(value)
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;');
}
function imageFrame(element, options) {
	if (element.fill?.type !== 'image') return '';
	if (!options.imageSize)
		fail(
			'FrameAssetRequiredError',
			element.elementId + '.fill',
			'SVG export of image frame requires imageSize',
		);
	const [iw, ih] = options.imageSize,
		crop = { top: 0, right: 0, bottom: 0, left: 0, ...element.fill.crop },
		cw = iw * (1 - crop.left - crop.right),
		ch = ih * (1 - crop.top - crop.bottom);
	if (cw <= 0 || ch <= 0)
		fail('InvalidValueError', element.elementId + '.fill.crop', 'crop has no visible area');
	const width = element.bounds?.[2] || 640,
		height = element.bounds?.[3] || 360,
		fit = element.fill.fit?.mode || 'cover';
	checkEnum(fit, ['cover', 'contain', 'fill'], 'fill.fit.mode');
	return `<svg width="${width}" height="${height}" viewBox="${iw * crop.left} ${ih * crop.top} ${cw} ${ch}" preserveAspectRatio="${fit === 'fill' ? 'none' : fit === 'contain' ? 'xMidYMid meet' : 'xMidYMid slice'}" overflow="hidden" opacity="${element.fill.opacity ?? 1}"><image href="${escapeXml((options.resolveAsset || ((x) => x))(element.fill.src))}" width="${iw}" height="${ih}"/></svg>`;
}
function frameBorder(element, theme) {
	if (!element.border) return '';
	const b = element.border,
		width = b.width ?? 1,
		dash = b.style === 'dash' ? '5 3' : b.style === 'dot' ? '1 2' : '';
	return `<rect x="${width / 2}" y="${width / 2}" width="${(element.bounds?.[2] || 640) - width}" height="${(element.bounds?.[3] || 360) - width}" fill="none" stroke="${escapeXml(resolveColor(b.color || '#000000', theme))}" stroke-width="${width}"${dash ? ` stroke-dasharray="${dash}"` : ''}/>`;
}
export function renderChartSvg(element, theme = {}, options = {}) {
	const frame = imageFrame(element, options),
		chart = echarts.init(null, null, {
			renderer: 'svg',
			ssr: true,
			width: element.bounds?.[2] || 640,
			height: element.bounds?.[3] || 360,
		});
	try {
		chart.setOption(buildChartOption(element, theme));
		return chart
			.renderToSVGString()
			.replace(/(<svg[^>]*>)/, '$1' + frame)
			.replace(/<\/svg>\s*$/, frameBorder(element, theme) + '</svg>');
	} finally {
		chart.dispose();
	}
}
export function renderChart(container, element, theme = {}, options = {}) {
	const chart = echarts.init(container, null, {
		renderer: 'svg',
		width: element.bounds?.[2] || container.clientWidth || 640,
		height: element.bounds?.[3] || container.clientHeight || 360,
	});
	let imageLayer,
		cancelled = false;
	const oldBorder = container.style.border,
		oldShadow = container.style.boxShadow;
	if (element.border)
		container.style.border = `${element.border.width ?? 1}px ${{ dash: 'dashed', dot: 'dotted' }[element.border.style] || 'solid'} ${resolveColor(element.border.color || '#000000', theme)}`;
	if (element.shadow)
		container.style.boxShadow = `${element.shadow.offset?.[0] || 0}px ${element.shadow.offset?.[1] || 0}px ${element.shadow.blur}px ${resolveColor(element.shadow.color, theme)}`;
	try {
		chart.setOption(buildChartOption(element, theme));
	} catch (e) {
		chart.dispose();
		throw e;
	}
	const ready =
		element.fill?.type === 'image'
			? new Promise((resolve, reject) => {
					const img = new Image();
					img.onload = () => {
						options = { ...options, imageSize: [img.naturalWidth, img.naturalHeight] };
						if (!cancelled) {
							imageLayer = document.createElementNS(
								'http://www.w3.org/2000/svg',
								'svg',
							);
							imageLayer.setAttribute('width', String(element.bounds?.[2] || 640));
							imageLayer.setAttribute('height', String(element.bounds?.[3] || 360));
							imageLayer.style.cssText =
								'position:absolute;inset:0;pointer-events:none';
							imageLayer.innerHTML = imageFrame(element, options);
							container.prepend(imageLayer);
						}
						resolve();
					};
					img.onerror = () =>
						reject(new Error(`FrameAssetLoadError: ${element.fill.src}`));
					img.src = (options.resolveAsset || ((x) => x))(element.fill.src);
				})
			: Promise.resolve();
	return {
		ready,
		dispose: () => {
			cancelled = true;
			chart.dispose();
			imageLayer?.remove();
			container.style.border = oldBorder;
			container.style.boxShadow = oldShadow;
		},
		resize: () => chart.resize(),
		getSvg: () => renderChartSvg(element, theme, options),
		instance: chart,
	};
}
