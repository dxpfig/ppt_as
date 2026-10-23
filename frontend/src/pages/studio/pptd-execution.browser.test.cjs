const assert = require('node:assert/strict');
const http = require('node:http');
const { test } = require('node:test');
const { chromium } = require('playwright');

const phases = ['context', 'plan', 'assets', 'pages', 'validate', 'export', 'visual', 'done'];
const labels = [
	'读取材料',
	'内容策划',
	'准备素材',
	'逐页创作',
	'检查修复',
	'渲染导出',
	'AI 视觉审稿',
	'完成交付',
];
const nodes = (current, terminal = false) =>
	phases.map((id, index) => ({
		id,
		label: labels[index],
		status:
			terminal || index < phases.indexOf(current)
				? 'completed'
				: id === current
					? 'running'
					: 'pending',
		summary: id === current ? '公开阶段状态' : undefined,
	}));

test('execution timeline follows live SSE, page retries and repairs, bounded history and mobile layout', async () => {
	const now = Date.now() / 1000;
	const project = {
		id: 'timeline-project',
		title: '过程验收',
		slides: [],
		messages: [],
		versions: [],
		theme: 'forest',
		phase: 'generating',
		workflow: 'pptd-v2',
		activeJobId: 'timeline-job',
	};
	let job = {
		id: 'timeline-job',
		status: 'running',
		stage: 5,
		message: '正在读取材料',
		createdAt: now - 900,
		updatedAt: now,
		recoverable: true,
		execution: {
			phase: 'context',
			nodes: nodes('context'),
			pages: [],
			events: [
				{ timestamp: now, phase: 'context', status: 'started', message: '正在读取材料' },
			],
		},
	};
	const clients = new Set();
	const server = http.createServer((request, response) => {
		response.writeHead(200, {
			'Content-Type': 'text/event-stream',
			'Cache-Control': 'no-cache',
			'Access-Control-Allow-Origin': '*',
		});
		clients.add(response);
		response.write('data: ' + JSON.stringify(job) + '\n\n');
		request.on('close', () => clients.delete(response));
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const streamUrl = 'http://127.0.0.1:' + server.address().port + '/events';
	const browser = await chromium.launch({
		executablePath: process.env.CHROMIUM_PATH || '/snap/bin/chromium',
		headless: true,
		args: ['--no-sandbox'],
	});
	const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
	const mutations = [];
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.message));
	const publish = (update) => {
		job = { ...job, ...update, updatedAt: Date.now() / 1000 };
		for (const response of clients) response.write('data: ' + JSON.stringify(job) + '\n\n');
	};
	await page.route('**/api/studio/**', async (route) => {
		const request = route.request();
		const path = new URL(request.url()).pathname.replace('/api/studio', '');
		if (!['GET', 'HEAD'].includes(request.method())) mutations.push(path);
		const json = (value) =>
			route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) });
		if (path === '/health')
			return json({
				llmConfigured: true,
				model: 'Configured model',
				rendererAvailable: true,
			});
		if (path === '/templates' || path === '/projects') return json([]);
		if (path === '/projects/timeline-project') return json(project);
		if (path === '/jobs/timeline-job') return json(job);
		if (path === '/jobs/timeline-job/events') return route.continue({ url: streamUrl });
		return route.fulfill({ status: 404, body: 'not found' });
	});
	try {
		await page.goto(
			(process.env.PPTD_UI_URL || 'http://127.0.0.1:5174') + '/?project=timeline-project',
		);
		const panel = page.getByRole('region', { name: '当前任务状态' });
		await panel.getByRole('button', { name: '读取材料：进行中', exact: true }).waitFor();
		await page.waitForFunction(
			() => document.querySelector('.pptd-execution-timeline')?.children.length === 8,
		);
		assert.equal(clients.size, 1, 'The test receives updates from an actual event stream');
		publish({
			message: '内容策划正在等待模型返回',
			execution: {
				phase: 'plan',
				nodes: nodes('plan'),
				pages: [],
				events: [
					{
						timestamp: now + 1,
						phase: 'plan',
						status: 'waiting',
						message: '等待模型返回内容规划',
						reasoning: 'PRIVATE_FIELD_MUST_NOT_RENDER',
					},
				],
				modelResponse: 'PRIVATE_FIELD_MUST_NOT_RENDER',
			},
		});
		await panel.getByRole('button', { name: '内容策划：进行中', exact: true }).waitFor();
		assert.equal(
			await panel.getByRole('button', { name: '准备素材：待开始', exact: true }).count(),
			1,
		);
		assert.match(await panel.innerText(), /最近状态更新/);
		assert.doesNotMatch(
			await page.locator('body').innerText(),
			/PRIVATE_FIELD_MUST_NOT_RENDER/,
		);
		let pages = Array.from({ length: 60 }, (_, index) => ({
			path: 'pages/' + index + '.page',
			index,
			title: '页面 ' + (index + 1),
			status: index < 11 ? 'completed' : index === 11 ? 'running' : 'pending',
			attempt: index === 11 ? 2 : 1,
			message: index === 11 ? '等待模型返回页面结果' : '',
			updatedAt: now + 2,
		}));
		const history = Array.from({ length: 30 }, (_, index) => ({
			timestamp: now + index,
			phase: 'pages',
			status: 'waiting',
			message: '公开页面事件 ' + index,
			pagePath: 'pages/11.page',
			attempt: 2,
			detail: index === 0 ? 'deck/pages/11.page' : undefined,
		}));
		publish({
			stage: 91,
			message: '正在创作第 12/60 页',
			checkpoint: { stage: 'page', completedPages: 11, totalPages: 60 },
			execution: { phase: 'pages', nodes: nodes('pages'), pages, events: history },
		});
		await panel.getByRole('button', { name: '逐页创作：进行中', exact: true }).waitFor();
		assert.match(await panel.innerText(), /已保存 11\/60 页/);
		assert.equal(
			await panel.getByRole('button', { name: '渲染导出：待开始', exact: true }).count(),
			1,
		);
		let box = await panel.boundingBox();
		assert.ok(box.y + box.height < 430, 'Collapsed overview leaves the preview visible');
		await panel.getByRole('button', { name: '逐页创作：进行中', exact: true }).click();
		const pageStates = panel.getByRole('list', { name: '逐页状态' });
		assert.equal(await pageStates.locator('li').count(), 60);
		assert.equal(await pageStates.locator('[data-page-path="pages/11.page"]').count(), 1);
		assert.match(
			await pageStates.locator('[data-page-path="pages/11.page"]').innerText(),
			/第 12 页.*页面 12/s,
		);
		assert.match(
			await pageStates.locator('[data-page-path="pages/11.page"]').innerText(),
			/第 2 次尝试/,
		);
		assert.ok(
			await panel
				.locator('.pptd-execution-details')
				.evaluate((element) => element.scrollHeight > element.clientHeight),
		);
		const actionLog = panel.getByRole('region', { name: '具体执行记录' });
		assert.equal(await actionLog.locator('li').count(), 30);
		assert.match(await actionLog.innerText(), /公开页面事件 0/);
		assert.match(await actionLog.innerText(), /deck\/pages\/11\.page/);
		assert.match(await actionLog.innerText(), /间隔|已等待/);
		assert.ok(
			(await panel.boundingBox()).height <= 380,
			'Expanded details leave room for the preview',
		);
		pages = pages.map((page) =>
			page.index === 11
				? { ...page, status: 'failed', attempt: 3, message: '页面返回格式不完整' }
				: page,
		);
		publish({
			status: 'failed',
			message: '第 12 页生成失败，已保留检查点。',
			error: '模型未返回符合约定的页面结果。' + '可从检查点继续。'.repeat(50),
			execution: {
				phase: 'pages',
				nodes: nodes('pages').map((node) =>
					node.id === 'pages' ? { ...node, status: 'failed' } : node,
				),
				pages,
				events: [
					...history,
					{
						timestamp: now + 31,
						phase: 'pages',
						status: 'failed',
						pagePath: 'pages/11.page',
						attempt: 3,
						message: '第 12 页结果校验失败',
					},
				],
			},
		});
		await panel.getByRole('button', { name: '从检查点继续', exact: true }).waitFor();
		box = await panel.getByRole('button', { name: '从检查点继续', exact: true }).boundingBox();
		assert.ok(
			box.y >= 0 && box.y + box.height <= 720,
			'Recovery action stays visible with long diagnostics and 60 pages',
		);
		assert.equal(await pageStates.locator('[data-page-path="pages/11.page"]').count(), 1);
		assert.match(
			await pageStates.locator('[data-page-path="pages/11.page"]').innerText(),
			/第 3 次尝试/,
		);
		await panel.getByRole('button', { name: '收起详情', exact: true }).click();
		const overviewHeight = (await panel.boundingBox()).height;
		publish({
			diagnostic: {
				code: 'render_validation',
				phase: 'validate',
				modelResponse: 'PRIVATE_FIELD_MUST_NOT_RENDER',
				issues: Array.from({ length: 18 }, (_, index) => ({
					page: index === 0 ? 'pages/unmatched.page' : 'pages/11.page',
					...(index === 2 ? { pageIndex: 4, pageTitle: '显式页面标题' } : {}),
					location: index === 0 ? 'pages/unmatched.page#title' : 'pages/11.page#title',
					elementId: 'title',
					code: 'text-overflow',
					message: '文字超出文本框，需要调整布局 ' + (index + 1),
					reasoning: 'PRIVATE_FIELD_MUST_NOT_RENDER',
				})),
			},
		});
		// Terminal jobs stop their event stream; reopening reads the final diagnostic snapshot.
		await page.reload();
		const diagnosticToggle = panel.getByRole('button', {
			name: '查看具体问题（18项）',
			exact: true,
		});
		await diagnosticToggle.waitFor();
		assert.equal(await panel.getByRole('region', { name: '具体问题', exact: true }).count(), 0);
		assert.equal(
			(await panel.boundingBox()).height,
			overviewHeight,
			'Collapsed diagnostics must not increase overview height',
		);
		await diagnosticToggle.click();
		const diagnosticRegion = panel.getByRole('region', { name: '具体问题', exact: true });
		const issueRows = diagnosticRegion.locator('li');
		assert.equal(await issueRows.count(), 18);
		assert.doesNotMatch(await issueRows.nth(0).innerText(), /第 .* 页/);
		assert.match(await issueRows.nth(0).innerText(), /具体位置：pages\/unmatched.page#title/);
		assert.match(await issueRows.nth(1).innerText(), /第 12 页 · 页面 12/);
		assert.match(await issueRows.nth(2).innerText(), /第 5 页 · 显式页面标题/);
		assert.doesNotMatch(
			await page.locator('body').innerText(),
			/PRIVATE_FIELD_MUST_NOT_RENDER/,
		);
		assert.ok(
			await diagnosticRegion.evaluate(
				(element) => element.scrollHeight > element.clientHeight,
			),
		);
		await page.setViewportSize({ width: 800, height: 600 });
		const recovery = panel.getByRole('button', { name: '从检查点继续', exact: true });
		assert.ok(
			await recovery.evaluate((element) => {
				const rect = element.getBoundingClientRect();
				const hit = document.elementFromPoint(
					rect.x + rect.width / 2,
					rect.y + rect.height / 2,
				);
				return rect.bottom <= innerHeight && (hit === element || element.contains(hit));
			}),
			'Expanded diagnostic issues must preserve a reachable recovery action',
		);
		await page.screenshot({ path: '/tmp/pptd-execution-diagnostic-narrow.png' });
		await page.setViewportSize({ width: 1280, height: 720 });
		await page.screenshot({ path: '/tmp/pptd-execution-failed-desktop.png' });
		await page.setViewportSize({ width: 390, height: 844 });
		await page.evaluate(() => window.scrollTo(0, 0));
		box = await panel.boundingBox();
		assert.ok(
			box.y >= 0 && box.y + box.height <= 844,
			'Mobile task panel precedes the long form',
		);
		assert.ok(
			await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
			'Mobile timeline must not overflow horizontally',
		);
		const formBox = await page.locator('.pptd-brief').boundingBox();
		assert.ok(formBox.y > box.y + box.height, 'Original form stays below the mobile workbench');
		await page.screenshot({ path: '/tmp/pptd-execution-failed-mobile.png', fullPage: true });
		// A fresh page simulates explicitly reopening a later task snapshot. No recovery is called.
		pages = pages.map((page) => ({
			...page,
			status: page.index === 11 ? 'running' : 'completed',
			attempt: page.index === 11 ? 4 : 1,
			message: page.index === 11 ? '正在修复布局问题' : '',
		}));
		publish({
			status: 'running',
			error: undefined,
			diagnostic: undefined,
			message: '正在检查并修复第 12 页',
			checkpoint: { stage: 'validate', completedPages: 60, totalPages: 60 },
			execution: {
				phase: 'validate',
				nodes: nodes('validate'),
				pages,
				events: [
					{
						timestamp: now + 32,
						phase: 'validate',
						status: 'retrying',
						pagePath: 'pages/11.page',
						attempt: 4,
						message: '正在修复第 12 页布局',
					},
				],
			},
		});
		await page.reload();
		await panel.getByRole('button', { name: '检查修复：进行中', exact: true }).click();
		const repairs = panel.getByRole('list', { name: '修复页面状态' });
		assert.equal(await repairs.locator('li').count(), 1);
		assert.match(await repairs.innerText(), /第 12 页/);
		assert.match(await repairs.innerText(), /第 4 次尝试/);
		publish({
			message: '正在导出 PPTX 和 PDF',
			execution: {
				phase: 'export',
				nodes: nodes('export'),
				pages: pages.map((page) => ({ ...page, status: 'completed' })),
				events: [
					{
						timestamp: now + 33,
						phase: 'export',
						status: 'started',
						message: '正在渲染导出',
					},
				],
			},
		});
		await panel.getByRole('button', { name: '渲染导出：进行中', exact: true }).waitFor();
		assert.match(await panel.innerText(), /已保存 60\/60 页/);
		assert.equal(
			await panel.getByRole('button', { name: '完成交付：待开始', exact: true }).count(),
			1,
		);
		publish({
			message: 'AI 正在检查整稿与每一页',
			execution: {
				phase: 'visual',
				nodes: nodes('visual'),
				pages,
				visual: { attempt: 1, covered: 1, total: 60, issueCount: 2, scope: 'page' },
				visualPages: [
					{
						path: 'pages/11.page',
						index: 11,
						title: '页面 12',
						status: 'completed',
						attempt: 1,
						issueCount: 2,
						message: '发现标题裁切，AI 将自动修复',
					},
				],
				events: [
					{
						phase: 'visual',
						status: 'completed',
						scope: 'page',
						pagePath: 'pages/11.page',
						pageIndex: 11,
						covered: 1,
						total: 60,
						issueCount: 2,
						message: '第 12 页视觉检查完成',
						reasoning: 'PRIVATE_FIELD_MUST_NOT_RENDER',
					},
				],
			},
		});
		await panel.getByRole('button', { name: 'AI 视觉审稿：进行中', exact: true }).click();
		const visualDetails = panel.getByRole('region', { name: 'AI 视觉审稿详情' });
		assert.match(await visualDetails.innerText(), /已审阅 1\/60 页/);
		assert.match(await visualDetails.innerText(), /第 1 轮/);
		const visualPages = visualDetails.getByRole('list', { name: '视觉审稿页面状态' });
		assert.equal(await visualPages.locator('li').count(), 1);
		assert.match(await visualPages.innerText(), /第 12 页/);
		assert.match(await visualPages.innerText(), /2 项问题/);
		assert.doesNotMatch(await panel.innerText(), /PRIVATE_FIELD_MUST_NOT_RENDER/);
		assert.equal(
			await panel.getByRole('button', { name: /人工审核|批准交付|确认通过/ }).count(),
			0,
		);
		publish({
			message: 'AI 正在修复后复审',
			execution: {
				...job.execution,
				visual: { attempt: 2, covered: 0, total: 60, issueCount: 0, scope: 'deck' },
				visualPages: [],
				events: [
					{
						phase: 'visual',
						status: 'started',
						scope: 'deck',
						attempt: 2,
						covered: 0,
						total: 60,
						message: 'AI 正在复审整稿',
					},
				],
			},
		});
		await visualDetails.getByText('第 2 轮 · 已审阅 0/60 页', { exact: true }).waitFor();
		assert.equal(await visualDetails.locator('.pptd-execution-page').count(), 0);
		await page.setViewportSize({ width: 1366, height: 768 });
		assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
		await page.screenshot({ path: '/tmp/pptd-visual-review-running.png' });
		await panel.getByRole('button', { name: 'AI 视觉审稿：进行中', exact: true }).focus();
		publish({
			status: 'completed',
			stage: 100,
			message: '演示已完成',
			result: {
				project: {
					...project,
					phase: 'ready',
					activeJobId: null,
					generationMeta: {
						validation: {
							visualReview: {
								status: 'passed',
								rounds: 2,
								reviewedPages: 60,
								totalPages: 60,
								issueCount: 0,
								repairedIssueCount: 2,
								summary: 'AI 已完成整稿与逐页视觉审稿',
							},
						},
					},
				},
			},
			execution: {
				phase: 'done',
				nodes: nodes('done', true),
				pages: pages.map((page) => ({ ...page, status: 'completed' })),
				events: [
					{
						timestamp: now + 34,
						phase: 'done',
						status: 'completed',
						message: '文件已就绪',
					},
				],
			},
		});
		await panel.getByRole('button', { name: '展开创作过程', exact: true }).waitFor();
		assert.equal(
			await panel
				.getByRole('button', { name: '展开创作过程', exact: true })
				.evaluate((element) => document.activeElement === element),
			true,
			'Completion returns keyboard focus to the persistent toggle',
		);
		assert.equal(
			await panel.locator('.pptd-execution-timeline').count(),
			0,
			'Completed work collapses by default',
		);
		await panel.getByRole('button', { name: '展开创作过程', exact: true }).click();
		assert.equal(await panel.getByRole('button', { name: /：已完成$/ }).count(), 8);
		assert.match(
			await panel.getByRole('status', { name: 'AI 视觉审稿结果' }).innerText(),
			/通过.*60\/60 页.*自动修复 2 项/s,
		);
		assert.deepEqual(
			mutations,
			[],
			'Reading execution details must never resume, retry, or alter a project',
		);
		assert.deepEqual(errors, []);
	} finally {
		await browser.close();
		for (const response of clients) response.end();
		await new Promise((resolve) => server.close(resolve));
	}
});
