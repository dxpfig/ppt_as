import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('./executionState.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
	compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
});
const { executionView } = await import(
	'data:text/javascript;base64,' + Buffer.from(outputText).toString('base64')
);
const job = (extra = {}) => ({
	id: 'j',
	status: 'running',
	stage: 91,
	message: '正在设计第 10/13 页',
	...extra,
});

test('explicit stages stay authoritative and percentages never invent completed nodes', () => {
	const view = executionView(
		job({
			execution: {
				phase: 'pages',
				nodes: [
					{ id: 'context', label: '读取材料', status: 'completed' },
					{ id: 'plan', label: '内容策划', status: 'completed' },
					{ id: 'assets', label: '准备素材', status: 'pending' },
					{ id: 'pages', label: '逐页创作', status: 'running' },
				],
				pages: [],
				events: [],
			},
		}),
	);
	assert.deepEqual(
		view.nodes.map((node) => node.id),
		['context', 'plan', 'assets', 'pages', 'validate', 'visual', 'export', 'done'],
	);
	assert.equal(view.phase, 'pages');
	assert.equal(view.nodes.find((node) => node.id === 'assets').status, 'pending');
	assert.equal(view.nodes.find((node) => node.id === 'export').status, 'unknown');
	assert.equal(view.source, 'execution');
});

test('legacy checkpoint exposes only its known current phase and saved count', () => {
	const view = executionView(
		job({
			status: 'failed',
			checkpoint: { stage: 'page', completedPages: 7, totalPages: 13 },
			events: [{ stage: 41, message: '正在设计第 10/13 页', timestamp: 100 }],
		}),
	);
	assert.equal(view.source, 'legacy');
	assert.equal(view.phase, 'pages');
	assert.equal(view.nodes.find((node) => node.id === 'pages').status, 'failed');
	assert.equal(view.nodes.filter((node) => node.status === 'completed').length, 0);
	assert.equal(view.saved, 7);
	assert.equal(view.total, 13);
	assert.deepEqual(view.pages, []);
	const uncertain = executionView(job({ stage: 88 }));
	assert.equal(uncertain.phase, 'export');
	assert.equal(uncertain.inferredPhase, true);
	assert.ok(uncertain.nodes.every((node) => node.status === 'unknown'));
});

test('latest page snapshot wins across retries and events remain bounded', () => {
	const view = executionView(
		job({
			execution: {
				phase: 'pages',
				nodes: [],
				pages: [
					{
						path: 'p/2.page',
						index: 1,
						title: '第二页',
						status: 'failed',
						attempt: 1,
						updatedAt: 10,
					},
					{
						path: 'p/1.page',
						index: 0,
						title: '第一页',
						status: 'completed',
						updatedAt: 20,
					},
					{
						path: 'p/2.page',
						index: 1,
						title: '第二页',
						status: 'running',
						attempt: 2,
						updatedAt: 30,
					},
				],
				events: Array.from({ length: 60 }, (_, index) => ({
					timestamp: index,
					phase: 'pages',
					status: 'running',
					message: '事件' + index,
				})),
			},
		}),
	);
	assert.equal(view.pages.length, 2);
	assert.equal(view.pages[1].status, 'running');
	assert.equal(view.pages[1].attempt, 2);
	assert.equal(view.saved, 1);
	assert.equal(view.total, null);
	assert.equal(view.events.length, 40);
	assert.equal(view.events.at(-1).message, '事件59');
});

test('all saved pages do not turn a running export into a finished delivery', () => {
	const view = executionView(
		job({
			checkpoint: { completedPages: 13, totalPages: 13 },
			execution: {
				phase: 'export',
				nodes: [
					{ id: 'pages', label: '逐页创作', status: 'completed' },
					{ id: 'export', label: '渲染导出', status: 'running' },
					{ id: 'done', label: '交付', status: 'pending' },
				],
				pages: [],
				events: [],
			},
		}),
	);
	assert.equal(view.saved, 13);
	assert.equal(view.phase, 'export');
	assert.equal(view.nodes.find((node) => node.id === 'done').status, 'pending');
});

test('bounded event display preserves known page repair associations', () => {
	const view = executionView(
		job({
			execution: {
				phase: 'export',
				nodes: [],
				pages: [],
				events: [
					{
						timestamp: 1,
						phase: 'validate',
						pagePath: 'pages/repaired.page',
						status: 'completed',
						message: '页面修复完成',
					},
					...Array.from({ length: 60 }, (_, index) => ({
						timestamp: index + 2,
						phase: 'export',
						status: 'waiting',
						message: '公开导出事件' + index,
					})),
				],
			},
		}),
	);
	assert.equal(view.events.length, 40);
	assert.deepEqual(view.repairPaths, ['pages/repaired.page']);
});

test('render tasks omit unexecuted generation stages and retain only real validation activity', () => {
	const render = job({
		kind: 'render',
		status: 'completed',
		stage: 100,
		execution: {
			phase: 'done',
			nodes: [
				...['context', 'plan', 'assets', 'pages', 'validate'].map((id) => ({
					id,
					label: id,
					status: 'pending',
				})),
				{ id: 'export', label: '渲染导出', status: 'completed' },
				{ id: 'done', label: '完成交付', status: 'completed' },
			],
			events: [{ phase: 'export', status: 'completed', message: '导出文件已通过检查' }],
		},
	});
	const view = executionView(render);
	assert.deepEqual(
		view.nodes.map(({ id, status }) => ({ id, status })),
		[
			{ id: 'export', status: 'completed' },
			{ id: 'done', status: 'completed' },
		],
	);
	assert.equal(render.execution.nodes[0].status, 'pending');
	const withValidation = executionView({
		...render,
		execution: {
			...render.execution,
			events: [{ phase: 'validate', status: 'started', message: '检查源文档' }],
		},
	});
	assert.deepEqual(
		withValidation.nodes.map((node) => node.id),
		['validate', 'export', 'done'],
	);
	assert.equal(
		withValidation.nodes[0].status,
		'pending',
		'Events must not overwrite explicit node status',
	);
	const activeValidation = executionView({
		...render,
		status: 'running',
		execution: {
			...render.execution,
			phase: 'validate',
			nodes: [{ id: 'validate', label: '检查修复', status: 'running' }],
			events: [],
		},
	});
	assert.equal(activeValidation.phase, 'validate');
	assert.deepEqual(
		activeValidation.nodes.map((node) => node.id),
		['validate', 'export', 'done'],
	);
	assert.equal(activeValidation.nodes[0].status, 'running');
	for (const status of ['failed', 'cancelled']) {
		const earlyFailure = executionView({
			...render,
			status,
			execution: {
				phase: 'context',
				nodes: [{ id: 'context', label: '读取材料', status }],
				events: [],
			},
		});
		assert.deepEqual(
			earlyFailure.nodes.map((node) => node.id),
			['context', 'export', 'done'],
		);
		assert.equal(earlyFailure.nodes[0].status, status);
	}
	const inferred = executionView(job({ kind: 'render', stage: 65 }));
	assert.deepEqual(
		inferred.nodes.map((node) => node.id),
		['export', 'done'],
	);
	assert.ok(inferred.nodes.every((node) => node.status === 'unknown'));
});

test('visual review tracks actual image coverage separately from saved source pages', () => {
	const view = executionView(
		job({
			kind: 'render',
			checkpoint: { completedPages: 2, totalPages: 2 },
			execution: {
				phase: 'visual',
				nodes: [{ id: 'visual', label: 'AI 视觉审稿', status: 'running' }],
				pages: [{ path: 'p/1.page', index: 0, title: '封面', status: 'completed' }],
				visualPages: [
					{ path: 'p/1.page', index: 0, title: '封面', status: 'running', attempt: 2 },
				],
				visual: { covered: 0, total: 2, attempt: 2, scope: 'deck', issueCount: 1 },
				events: [],
			},
		}),
	);
	assert.equal(view.phase, 'visual');
	assert.equal(view.nodes.find((n) => n.id === 'visual').status, 'running');
	assert.equal(view.saved, null);
	assert.equal(view.visualPages[0].status, 'running');
	assert.equal(view.pages[0].status, 'completed');
	assert.deepEqual(view.visual, {
		covered: 0,
		total: 2,
		attempt: 2,
		scope: 'deck',
		issueCount: 1,
	});
});

test('only an explicit passed visual report with all pages reviewed is shown as passed', () => {
	const summary = {
		status: 'passed',
		rounds: 2,
		reviewedPages: 13,
		totalPages: 13,
		issueCount: 0,
		repairedIssueCount: 3,
		summary: '整稿及逐页视觉审稿通过',
	};
	const reportJob = (visualReview) =>
		job({
			status: 'completed',
			result: { project: { generationMeta: { validation: { visualReview } } } },
		});
	assert.deepEqual(executionView(reportJob(summary)).visualReport, summary);
	assert.equal(executionView(job({ status: 'completed' })).visualReport, null);
	assert.equal(executionView(reportJob({ ...summary, reviewedPages: 12 })).visualReport, null);
	assert.equal(executionView(reportJob({ ...summary, issueCount: 1 })).visualReport, null);
});

test('render-only review never advertises empty generation checkpoint counters', () => {
	const view = executionView(
		job({
			kind: 'render',
			status: 'completed',
			checkpoint: { completedPages: 0, totalPages: 0 },
			result: {
				project: {
					pptd: { pageCount: 13 },
					generationMeta: {
						validation: {
							visualReview: {
								status: 'passed',
								rounds: 1,
								reviewedPages: 13,
								totalPages: 13,
								issueCount: 0,
							},
						},
					},
				},
			},
		}),
	);
	assert.equal(view.saved, null);
	assert.equal(view.visualReport.reviewedPages, 13);
	assert.equal(view.visualReport.totalPages, 13);
});
