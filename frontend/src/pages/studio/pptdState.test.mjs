import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';

import ts from 'typescript';

const source = readFileSync(new URL('./pptdState.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
	compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
});
const {
	acceptServerProject,
	generationOptions,
	groupImportWarnings,
	isLegacyProject,
	isRunningJob,
	pageTitle,
	pagePreviewSources,
} = await import('data:text/javascript;base64,' + Buffer.from(outputText).toString('base64'));

const project = (overrides = {}) => ({
	id: 'p',
	title: '演示',
	slides: [],
	messages: [],
	versions: [],
	theme: 'forest',
	phase: 'ready',
	...overrides,
});

test('unexported pages have a current-page read-only thumbnail source', () => {
	const p = project({
		pptd: { revision: 11, viewUrl: '/api/studio/projects/p/pptd/view?mode=edit' },
	});
	const source = pagePreviewSources(p, 'pages/15 final.page', 14, 'http://localhost:5174');
	assert.equal(source.image, undefined);
	const url = new URL(source.view, 'http://localhost:5174');
	assert.equal(url.searchParams.get('page'), 'pages/15 final.page');
	assert.equal(url.searchParams.get('revision'), '11');
	assert.equal(url.searchParams.get('mode'), 'preview');
	assert.equal(url.searchParams.get('thumbnail'), '1');
});

test('thumbnails use only matching export images and always retain a live fallback', () => {
	const p = project({
		pptd: { revision: 12, viewUrl: '/api/studio/projects/p/pptd/view' },
		artifact: { revision: 11, previewUrls: ['/old.png'] },
	});
	assert.equal(pagePreviewSources(p, 'pages/1.page', 0, 'http://localhost').image, undefined);
	p.artifact.revision = 12;
	assert.equal(pagePreviewSources(p, 'pages/1.page', 0, 'http://localhost').image, '/old.png');
	assert.ok(pagePreviewSources(p, 'pages/1.page', 0, 'http://localhost').view);
	assert.equal(pagePreviewSources(p, 'pages/2.page', 1, 'http://localhost').image, undefined);
});

test('automatic page count remains null and every generation preference is sent', () => {
	const options = generationOptions({
		pageCount: 'auto',
		audience: '同事',
		workflow: 'new',
		designMode: 'style-reference',
		usage: 'reading',
		scenario: 'tech-engineering',
		allowResearch: true,
		speakerNotes: true,
		animationRequested: true,
		designSystem: '简洁深蓝',
		references: '参考材料',
	});
	assert.equal(options.pageCount, null);
	assert.equal(options.designMode, 'style-reference');
	assert.equal(options.scenario, 'tech-engineering');
	assert.equal(options.animationRequested, true);
	assert.equal(options.speakerNotes, true);
	assert.equal(options.allowResearch, true);
	assert.equal(generationOptions({ pageCount: '18' }).pageCount, 18);
	assert.throws(() => generationOptions({ pageCount: '0' }), /页数/);
});

test('server PPTD is authoritative and stale exports are invalidated after an edit', () => {
	const previous = project({ pptd: { revision: 1 }, artifact: { id: 'export-1' } });
	const next = acceptServerProject(
		previous,
		project({
			pptd: { revision: 2 },
			artifact: { id: 'export-1' },
			slides: [{ title: 'legacy' }],
		}),
	);
	assert.equal(next.pptd.revision, 2);
	assert.equal(next.artifact, undefined);
	const exported = acceptServerProject(
		next,
		project({ pptd: { revision: 2 }, artifact: { id: 'export-2', revision: 2 } }),
	);
	assert.equal(exported.artifact.id, 'export-2');
});

test('PPTD projects never become legacy just because compatibility slides exist', () => {
	assert.equal(isLegacyProject(project({ slides: [{ id: 'old' }] })), true);
	assert.equal(
		isLegacyProject(project({ pptd: { revision: 1 }, slides: [{ id: 'old' }] })),
		false,
	);
	assert.equal(isLegacyProject(project()), false);
});

test('template import warnings are grouped without losing their scope', () => {
	const groups = groupImportWarnings([
		'Office visual effects are not imported.',
		'Office visual effects are not imported.',
		'Source timing tree not imported.',
		'  ',
		'Source timing tree not imported.',
	]);
	assert.deepEqual(groups, [
		{ message: 'Office visual effects are not imported.', count: 2 },
		{ message: 'Source timing tree not imported.', count: 2 },
	]);
	assert.deepEqual(groupImportWarnings(undefined), []);
});

test('interrupted jobs are terminal and page titles come from PPTD content', () => {
	assert.equal(isRunningJob({ status: 'interrupted' }), false);
	assert.equal(isRunningJob({ status: 'running' }), true);
	assert.equal(pageTitle({ path: 'pages/one.pptd', data: { name: 'Overview' } }, 0), 'Overview');
	assert.equal(pageTitle({ path: 'pages/one.pptd', data: {} }, 0), '第 1 页');
});

test('speaker notes are opt-in for new presentations', () => {
	assert.equal(generationOptions({}).speakerNotes, false);
	assert.equal(generationOptions({ speakerNotes: true }).speakerNotes, true);
});

test('real PPTD text content supplies a plain title, prioritizing title element IDs', () => {
	const page = {
		path: 'pages/real.page',
		data: {
			elements: [
				{ elementId: 'eyebrow', elementType: 'text', content: { text: '季度复盘' } },
				{ elementId: 'subtitle', elementType: 'text', content: { text: '次要说明' } },
				{
					elementId: 'title',
					elementType: 'text',
					content: {
						text: '<p>营收 <strong>增长</strong> &amp; 效率</p><p>提升 &#x32;0%</p><script>bad()</script>',
					},
				},
			],
		},
	};
	assert.equal(pageTitle(page, 0), '营收 增长 & 效率 提升 20%');
	assert.equal(
		pageTitle(
			{
				path: 'pages/fallback.page',
				data: {
					elements: [
						{ elementId: 'title', elementType: 'text', content: { text: '<p> </p>' } },
						{
							elementId: 'body',
							elementType: 'text',
							content: { text: '实际页面内容' },
						},
					],
				},
			},
			1,
		),
		'实际页面内容',
	);
	assert.equal(
		pageTitle(
			{
				path: 'pages/fallback.page',
				data: {
					elements: [
						{
							elementId: 'title',
							elementType: 'chart',
							content: { text: '图表内部数据不是页面标题' },
						},
					],
				},
			},
			2,
		),
		'第 3 页',
	);
});
