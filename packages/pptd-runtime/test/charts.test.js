import test from 'node:test';
import assert from 'node:assert/strict';

const mod = () => import('../src/charts.js');
const chart = (
	series,
	cols = ['x', 'a', 'b'],
	rows = [
		['A', 2, 6],
		['B', 4, 4],
	],
	extra = {},
) => ({
	elementType: 'chart',
	elementId: 'chart-test',
	bounds: [0, 0, 640, 360],
	data: { cols, rows },
	series,
	...extra,
});
const xy = (type, more = {}) => ({ type, encode: { x: 'x', y: 'a' }, ...more });

test('axis label rotation and category coverage and data label position reach rendering', async () => {
	const { buildChartOption, validateChart } = await mod();
	const source = chart([xy('bar')], undefined, undefined, {
		xAxis: { label: { rotation: 25, interval: 0 } },
		dataLabels: { show: true, position: 'outsideEnd' },
	});
	const before = structuredClone(source),
		option = buildChartOption(source, {});
	assert.equal(option.xAxis[0].axisLabel.rotate, 25);
	assert.equal(option.xAxis[0].axisLabel.interval, 0);
	assert.equal(option.series[0].label.position, 'top');
	assert.deepEqual(source, before);
	for (const label of [{ rotation: 100 }, { interval: -1 }, { interval: 1.5 }])
		assert.throws(() => validateChart({ ...source, xAxis: { label } }), /InvalidValueError/);
	assert.throws(
		() => validateChart({ ...source, dataLabels: { position: 'unknown' } }),
		/InvalidValueError/,
	);
});

test('normalization parses numeric strings without changing source and rejects corrupt data', async () => {
	const { validateChart } = await mod();
	const source = chart(
		[xy('line')],
		['x', 'a'],
		[
			['A', '2.5'],
			['B', null],
		],
	);
	assert.deepEqual(
		validateChart(source).series[0].points.map((p) => p.y),
		[2.5, null],
	);
	assert.equal(source.data.rows[0][1], '2.5');
	for (const value of ['abc', '', '  ', true, Infinity])
		assert.throws(
			() => validateChart(chart([xy('line')], ['x', 'a'], [['A', value]])),
			/NonNumericValueError|InvalidValueError/,
		);
	assert.throws(
		() => validateChart(chart([xy('bar', { encode: { x: 'x', y: 'missing' } })])),
		/UnknownColumnError/,
	);
	assert.throws(
		() => validateChart(chart([xy('bar')], ['x', 'x'], [['A', 1]])),
		/DuplicateColumnError/,
	);
});

test('series defaults merge only one object level and global labels inherit independently', async () => {
	const { buildChartOption } = await mod();
	const option = buildChartOption(
		chart(
			[xy('scatter', { marker: { size: 12 }, dataLabels: { show: true } })],
			['x', 'a'],
			[[1, 2]],
			{
				seriesDefaults: {
					scatter: { marker: { shape: 'diamond', fill: '$primary', size: 8 } },
				},
				dataLabels: { color: '$primary', fontSize: 15 },
			},
		),
		{ colors: { primary: '#123456' } },
	);
	assert.equal(option.series[0].symbol, 'diamond');
	assert.equal(option.series[0].symbolSize, 12);
	assert.equal(option.series[0].itemStyle.color, '#123456');
	assert.equal(option.series[0].label.color, '#123456');
	assert.equal(option.series[0].label.fontSize, 15);
	assert.throws(
		() =>
			buildChartOption(
				chart([xy('line')], undefined, undefined, {
					seriesDefaults: { line: { encode: { y: 'b' } } },
				}),
			),
		/InvalidValueError/,
	);
});

test('type mixing, stack groups and secondary axes are enforced', async () => {
	const { validateChart, buildChartOption } = await mod();
	assert.throws(
		() =>
			validateChart(
				chart([xy('bar'), { type: 'pie', encode: { category: 'x', value: 'a' } }]),
			),
		/TypeMixingError/,
	);
	assert.throws(
		() =>
			validateChart(chart([xy('bar', { stack: 'value' }), xy('bar', { stack: 'percent' })])),
		/StackModeMismatchError/,
	);
	assert.throws(() => validateChart(chart([xy('line', { yAxisIndex: 1 })])), /AxisIndexError/);
	const option = buildChartOption(
		chart([xy('bar'), xy('line', { yAxisIndex: 1 })], undefined, undefined, {
			yAxis: [{}, { min: 0, max: 10 }],
		}),
	);
	assert.equal(option.yAxis[1].position, 'right');
	assert.equal(option.series[1].yAxisIndex, 1);
	assert.equal(option.yAxis[1].max, 10);
});

test('percent stacks use category totals and stream area uses a centered baseline', async () => {
	const { buildChartOption } = await mod();
	const option = buildChartOption(
		chart([
			xy('bar', { stack: 'percent' }),
			xy('bar', { encode: { x: 'x', y: 'b' }, stack: 'percent' }),
		]),
	);
	assert.deepEqual(
		option.series.map((s) => s.data.map((p) => p.value[1])),
		[
			[0.25, 0.5],
			[0.75, 0.5],
		],
	);
	const stream = buildChartOption(
		chart([
			xy('area', { stack: 'stream' }),
			xy('area', { encode: { x: 'x', y: 'b' }, stack: 'stream' }),
		]),
	);
	assert.equal(stream.series[0].type, 'custom');
	assert.deepEqual(
		stream.series[0].data.map((p) => p.value.slice(1)),
		[
			[-4, -2],
			[-4, 0],
		],
	);
	assert.deepEqual(
		stream.series[1].data.map((p) => p.value.slice(1)),
		[
			[-2, 4],
			[0, 4],
		],
	);
});

test('first explicit nullHandling applies across linear series and gap remains missing', async () => {
	const { buildChartOption } = await mod();
	const option = buildChartOption(
		chart(
			[
				xy('line', { nullHandling: 'zero' }),
				xy('area', { encode: { x: 'x', y: 'b' }, nullHandling: 'connect' }),
			],
			['x', 'a', 'b'],
			[['A', null, null]],
		),
	);
	assert.deepEqual(
		option.series.map((s) => s.data[0].value[1]),
		[0, 0],
	);
	const gap = buildChartOption(chart([xy('line')], ['x', 'a'], [['A', null]]));
	assert.equal(gap.series[0].data[0].value[1], null);
	assert.equal(gap.series[0].connectNulls, false);
});

test('scatter filter uses exact matching and negative bubble sizes clamp to zero', async () => {
	const { buildChartOption, validateChart } = await mod();
	const source = chart(
		[xy('scatter', { dataFilter: { col: 'group', value: 'one' } })],
		['x', 'a', 'group'],
		[
			[1, 2, 'one'],
			[3, 4, 'two'],
		],
	);
	assert.deepEqual(
		buildChartOption(source).series[0].data.map((p) => p.value),
		[[1, 2]],
	);
	const bubble = validateChart(
		chart(
			[{ type: 'bubble', encode: { x: 'x', y: 'a', size: 'b' } }],
			['x', 'a', 'b'],
			[[1, 2, -10]],
		),
	);
	assert.equal(bubble.series[0].points[0].size, 0);
	assert.throws(
		() => validateChart(chart([xy('scatter', { marker: false })], ['x', 'a'], [[1, 2]])),
		/InvalidValueError/,
	);
});

test('HLC has wick and close point without a body, OHLC preserves order', async () => {
	const { buildChartOption } = await mod();
	const source = chart(
		[{ type: 'candlestick', encode: { x: 'x', high: 'h', low: 'l', close: 'c', open: 'o' } }],
		['x', 'h', 'l', 'c', 'o'],
		[['Mon', 9, 1, 7, 3]],
	);
	assert.deepEqual(buildChartOption(source).series[0].data[0].value, ['Mon', 3, 7, 1, 9]);
	delete source.series[0].encode.open;
	const hlc = buildChartOption(source);
	assert.equal(hlc.series[0].type, 'custom');
	assert.deepEqual(hlc.series[0].data[0].value, ['Mon', 1, 9, 7]);
});

test('waterfall totals reset cumulative baseline and mismatch is an explicit warning', async () => {
	const { validateChart, buildChartOption } = await mod();
	const source = chart(
		[{ type: 'waterfall', encode: { x: 'x', y: 'a', isTotal: 'total' } }],
		['x', 'a', 'total'],
		[
			['Start', 10, true],
			['Drop', -3, false],
			['End', 7, true],
		],
	);
	assert.deepEqual(
		buildChartOption(source).series[0].data.map((p) => p.value.slice(1)),
		[
			[0, 10],
			[10, 7],
			[0, 7],
		],
	);
	source.data.rows[2][1] = 8;
	assert.equal(validateChart(source).warnings[0].code, 'WaterfallTotalMismatchWarning');
	source.data.rows[2][2] = 'true';
	assert.throws(() => validateChart(source), /InvalidValueError/);
});

test('heatmap preserves first appearance, leaves missing cells transparent and centers diverging colors', async () => {
	const { buildChartOption } = await mod();
	const option = buildChartOption(
		chart(
			[
				{
					type: 'heatmap',
					encode: { x: 'x', y: 'y', value: 'v' },
					colorScale: { type: 'diverging' },
					colorScheme: ['#ff0000', '#ffffff', '#0000ff'],
				},
			],
			['x', 'y', 'v'],
			[
				['B', 'Two', -2],
				['A', 'One', 5],
				['B', 'One', null],
			],
		),
	);
	assert.deepEqual(option.xAxis[0].data, ['B', 'A']);
	assert.deepEqual(option.yAxis[0].data, ['Two', 'One']);
	assert.deepEqual(
		option.series[0].data.map((p) => p.value),
		[
			[0, 0, -2],
			[1, 1, 5],
		],
	);
	assert.equal(option.visualMap.min, -5);
	assert.equal(option.visualMap.max, 5);
});

test('tree relationships and depth colors are meaningful, cycles and orphans fail', async () => {
	const { buildChartOption, validateChart } = await mod();
	const source = chart(
		[
			{
				type: 'treemap',
				encode: { category: 'name', value: 'v', parent: 'parent' },
				fill: [['#808080', '#ff0000']],
			},
		],
		['name', 'v', 'parent'],
		[
			['Root', 10, null],
			['Child', 10, 'Root'],
			['Leaf', 10, 'Child'],
		],
	);
	const root = buildChartOption(source).series[0].data[0];
	assert.equal(root.itemStyle.color, '#808080');
	assert.equal(root.children[0].itemStyle.color, '#ff0000');
	assert.equal(root.children[0].children[0].itemStyle.color, '#cc0000');
	source.data.rows[0][2] = 'Leaf';
	assert.throws(() => validateChart(source), /CyclicGraphError/);
	source.data.rows[0][2] = 'Absent';
	assert.throws(() => validateChart(source), /UnknownParentError/);
});

test('sankey node colors follow stable topological order and rejects directed cycles', async () => {
	const { buildChartOption, validateChart } = await mod();
	const source = chart(
		[
			{
				type: 'sankey',
				encode: { source: 'from', target: 'to', flow: 'v' },
				fill: ['#ff0000', '#00ff00'],
			},
		],
		['from', 'to', 'v'],
		[
			['B', 'C', 2],
			['A', 'B', 2],
		],
	);
	const nodes = buildChartOption(source).series[0].data;
	assert.deepEqual(
		nodes.map((n) => n.name),
		['A', 'B', 'C'],
	);
	assert.deepEqual(
		nodes.map((n) => n.itemStyle.color),
		['#ff0000', '#00ff00', '#ff0000'],
	);
	source.data.rows.push(['C', 'A', 1]);
	assert.throws(() => validateChart(source), /CyclicGraphError/);
});

test('all thirteen chart types produce real SVG content', async () => {
	const { renderChartSvg } = await mod();
	const cases = [
		...['bar', 'line', 'area'].map((t) => chart([xy(t)])),
		chart(
			[xy('scatter')],
			['x', 'a'],
			[
				[1, 2],
				[2, 4],
			],
		),
		chart(
			[{ type: 'bubble', encode: { x: 'x', y: 'a', size: 'b' } }],
			['x', 'a', 'b'],
			[
				[1, 2, 4],
				[2, 4, 9],
			],
		),
		chart(
			[
				{
					type: 'candlestick',
					encode: { x: 'x', high: 'h', low: 'l', close: 'c', open: 'o' },
				},
			],
			['x', 'h', 'l', 'c', 'o'],
			[
				['A', 9, 1, 7, 3],
				['B', 9, 1, 3, 7],
			],
		),
		chart([{ type: 'pie', encode: { category: 'x', value: 'a' } }]),
		chart(
			[{ type: 'radar', encode: { category: 'x', y: 'a' } }],
			['x', 'a'],
			[
				['A', 2],
				['B', 3],
				['C', 4],
			],
		),
		chart([{ type: 'waterfall', encode: { x: 'x', y: 'a' } }]),
		chart(
			[{ type: 'heatmap', encode: { x: 'x', y: 'y', value: 'v' } }],
			['x', 'y', 'v'],
			[
				['A', 'X', 1],
				['B', 'Y', 3],
			],
		),
		...['treemap', 'sunburst'].map((t) =>
			chart([{ type: t, encode: { category: 'x', value: 'a' } }]),
		),
		chart(
			[{ type: 'sankey', encode: { source: 'x', target: 'y', flow: 'v' } }],
			['x', 'y', 'v'],
			[
				['A', 'B', 2],
				['A', 'C', 3],
			],
		),
	];
	for (const source of cases) {
		const svg = renderChartSvg(source);
		assert.match(svg, /<svg/);
		assert.match(svg, /<(path|rect|polygon|circle)[ >]/, source.series[0].type);
		assert.doesNotMatch(svg, /NaN|undefined/);
	}
});

test('radar missing spokes use an explicit gap or connection instead of a fabricated zero', async () => {
	const { buildChartOption, renderChartSvg } = await mod();
	const source = chart(
		[
			{
				type: 'radar',
				encode: { category: 'x', y: 'a' },
				nullHandling: 'connect',
				marker: { shape: 'diamond', size: 8 },
			},
		],
		['x', 'a'],
		[
			['A', 4],
			['B', null],
			['C', 2],
			['D', 3],
		],
	);
	assert.equal(buildChartOption(source).series[0].type, 'custom');
	assert.doesNotMatch(renderChartSvg(source), /NaN/);
	source.series[0].nullHandling = 'gap';
	const connected = renderChartSvg(source);
	assert.match(connected, /<path/);
});

test('heatmap linear default domain follows data rather than forcing zero', async () => {
	const { buildChartOption } = await mod();
	const source = chart(
		[{ type: 'heatmap', encode: { x: 'x', y: 'y', value: 'v' } }],
		['x', 'y', 'v'],
		[
			['A', 'One', 8],
			['B', 'One', 12],
		],
	);
	const option = buildChartOption(source);
	assert.equal(option.visualMap.min, 8);
	assert.equal(option.visualMap.max, 12);
});

test('custom and builtin ShapeDef pictographic bars render instead of ignoring shapeName', async () => {
	const { buildChartOption, renderChartSvg } = await mod();
	for (const symbol of [
		{ shapeName: 'triangle', adjustments: [30000] },
		{ shapeName: 'custom', viewBox: [100, 100], path: 'M0 100 L50 0 L100 100 Z' },
	]) {
		const source = chart([xy('bar', { symbol })]);
		assert.equal(buildChartOption(source).series[0].type, 'pictorialBar');
		assert.match(renderChartSvg(source), /<path/);
	}
});

test('candlestick independent wick style is represented by a distinct geometry', async () => {
	const { buildChartOption, renderChartSvg } = await mod();
	const source = chart(
		[
			{
				type: 'candlestick',
				encode: { x: 'x', open: 'o', high: 'h', low: 'l', close: 'c' },
				wickStyle: { color: '#123456', width: 3 },
				upBars: { fill: '#ff0000', border: { color: '#00ff00', width: 1 } },
			},
		],
		['x', 'o', 'h', 'l', 'c'],
		[['A', 2, 8, 1, 6]],
	);
	assert.equal(buildChartOption(source).series[0].type, 'custom');
	assert.match(renderChartSvg(source), /#123456/);
	assert.match(renderChartSvg(source), /#00ff00/);
});

test('SSR image chart frames require known dimensions and preserve crop and fit', async () => {
	const { renderChartSvg } = await mod();
	const source = chart([xy('bar')], undefined, undefined, {
		fill: {
			type: 'image',
			src: 'media/bg.png',
			crop: { left: 0.1, right: 0.2 },
			fit: { mode: 'contain' },
			opacity: 0.6,
		},
		border: { width: 2, color: '#123456' },
	});
	assert.throws(() => renderChartSvg(source), /FrameAssetRequiredError/);
	const svg = renderChartSvg(
		source,
		{},
		{ imageSize: [100, 80], resolveAsset: () => 'https://example.test/bg.png' },
	);
	assert.match(svg, /viewBox="10 0 70 80"/);
	assert.match(svg, /preserveAspectRatio="xMidYMid meet"/);
	assert.match(svg, /https:\/\/example.test\/bg.png/);
	assert.match(svg, /#123456/);
});

test('numeric axis-label formats reject unsupported Excel expressions', async () => {
	const { validateChart } = await mod();
	assert.throws(
		() =>
			validateChart(
				chart([xy('bar')], undefined, undefined, {
					yAxis: { label: { numberFormat: '[Red]0' } },
				}),
			),
		/InvalidValueError/,
	);
});

test('default bubble scale makes bubble area proportional to data size', async () => {
	const { buildChartOption } = await mod();
	const source = chart(
		[{ type: 'bubble', encode: { x: 'x', y: 'a', size: 'b' } }],
		['x', 'a', 'b'],
		[
			[1, 2, 1],
			[2, 3, 4],
		],
	);
	const size = buildChartOption(source).series[0].symbolSize;
	assert.equal(size([2, 3, 4]) / size([1, 2, 1]), 2);
});

test('gradient and theme references preserve alpha and reject invalid stops or cycles', async () => {
	const { buildChartOption } = await mod();
	const source = chart([
		xy('bar', {
			fill: {
				type: 'gradient',
				gradientType: 'linear',
				angle: 90,
				stops: [
					{ position: 0, color: '$accent' },
					{ position: 1, color: '#00000080' },
				],
			},
		}),
	]);
	const color = buildChartOption(source, { colors: { accent: '$primary', primary: '#123456' } })
		.series[0].itemStyle.color;
	assert.equal(color.colorStops[0].color, '#123456');
	assert.equal(color.colorStops[1].color, '#00000080');
	assert.throws(
		() => buildChartOption(source, { colors: { accent: '$primary', primary: '$accent' } }),
		/CyclicColorError/,
	);
	source.series[0].fill.stops[1].position = 2;
	assert.throws(
		() => buildChartOption(source, { colors: { accent: '#123456' } }),
		/InvalidValueError/,
	);
});

test('radar spoke axis renders formatted numeric ticks as well as categories', async () => {
	const { renderChartSvg } = await mod();
	const source = chart(
		[{ type: 'radar', encode: { category: 'x', y: 'a' } }],
		['x', 'a'],
		[
			['A', 2],
			['B', 4],
			['C', 6],
		],
		{ spokeAxis: { min: 0, max: 10, label: { numberFormat: '0.0' } } },
	);
	const svg = renderChartSvg(source);
	for (const number of ['0.0', '2.0', '4.0', '6.0', '8.0', '10.0'])
		assert.ok(svg.includes('>' + number + '</text>'), number);
});

test('invalid sizing never reaches a renderer as NaN or a negative stroke', async () => {
	const { validateChart } = await mod();
	for (const sizeRange of [
		['a', 'b'],
		[0, NaN],
		[5, 2],
	])
		assert.throws(
			() =>
				validateChart(
					chart(
						[{ type: 'bubble', encode: { x: 'x', y: 'a', size: 'b' }, sizeRange }],
						['x', 'a', 'b'],
						[[1, 2, 4]],
					),
				),
			/InvalidValueError/,
		);
	assert.throws(() => validateChart(chart([xy('line', { width: -2 })])), /InvalidValueError/);
	assert.throws(
		() => validateChart(chart([xy('line', { marker: { size: NaN } })])),
		/InvalidValueError/,
	);
	assert.throws(
		() => validateChart(chart([xy('bar')], undefined, undefined, { categoryGap: NaN })),
		/InvalidValueError/,
	);
});

test('stream area keeps authored labels and marker styling', async () => {
	const { renderChartSvg } = await mod();
	const source = chart(
		[
			xy('area', {
				stack: 'stream',
				dataLabels: { show: true, numberFormat: '0.0' },
				marker: { shape: 'circle', size: 9, fill: '#fedcba' },
			}),
		],
		['x', 'a'],
		[
			['A', 7],
			['B', 9],
		],
	);
	const svg = renderChartSvg(source);
	assert.ok(svg.includes('>7.0</text>'));
	assert.ok(svg.includes('>9.0</text>'));
	assert.ok(svg.includes('#fedcba'));
});

test('treemap never labels ECharts synthetic root as authored data', async () => {
	const { renderChartSvg } = await mod();
	const svg = renderChartSvg(
		chart(
			[
				{
					type: 'treemap',
					encode: { category: 'x', value: 'a' },
					dataLabels: { show: true },
				},
			],
			['x', 'a'],
			[
				['A', 7],
				['B', 3],
			],
		),
	);
	assert.ok(!svg.includes('>10</text>'));
});
