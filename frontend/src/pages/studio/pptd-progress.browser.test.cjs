const assert = require('node:assert/strict');
const { test } = require('node:test');
const { chromium } = require('playwright');

test('long-running progress and recoverable failures stay visible in the main workbench', async () => {
	const browser = await chromium.launch({
		executablePath: process.env.CHROMIUM_PATH || '/snap/bin/chromium',
		headless: true,
		args: ['--no-sandbox'],
	});
	const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
	const now = Date.now() / 1000;
	const project = {
		id: 'long-job-project',
		title: '长任务',
		slides: [],
		messages: [],
		versions: [],
		theme: 'forest',
		phase: 'generating',
		workflow: 'pptd-v2',
		activeJobId: 'long-job',
	};
	let job = {
		id: 'long-job',
		status: 'running',
		stage: 41,
		message: '正在设计第 10/13 页：团队与资源投入',
		createdAt: now - 933,
		updatedAt: now - 150,
		recoverable: true,
		checkpoint: { completedPages: 7, totalPages: 13, summary: '已保存 7/13 页' },
	};
	const mutations = [];
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
		if (path === '/projects/long-job-project') return json(project);
		if (path === '/jobs/long-job') return json(job);
		if (path.endsWith('/events'))
			return route.fulfill({ status: 404, body: 'polling fallback' });
		return route.fulfill({ status: 404, body: 'unhandled' });
	});
	try {
		await page.goto(
			(process.env.PPTD_UI_URL || 'http://127.0.0.1:5174') + '/?project=long-job-project',
		);
		const status = page
			.locator('.pptd-workbench')
			.getByRole('region', { name: '当前任务状态' });
		await status.waitFor({ timeout: 10000 });
		assert.match(await status.innerText(), /正在设计第 10\/13 页/);
		assert.match(await status.innerText(), /已保存 7\/13 页/);
		assert.match(await status.innerText(), /已用 15 分/);
		assert.match(await status.innerText(), /最近状态更新 2 分/);
		assert.match(await status.innerText(), /耗时不代表完成进度/);
		const position = await status.boundingBox();
		assert.ok(
			position.y >= 0 && position.y + position.height <= 720,
			'Progress must stay within the viewport without scrolling the form',
		);
		assert.equal(await status.getByRole('button', { name: '停止任务' }).count(), 1);
		job = {
			...job,
			status: 'failed',
			updatedAt: Date.now() / 1000,
			message: '模型未返回符合 JSON 契约的 page 结果；检查点可恢复。',
			error:
				"Expecting ',' delimiter line 1 col 846。" +
				'已保留可恢复的页面检查点。'.repeat(15),
		};
		await status.getByRole('button', { name: '从检查点继续' }).waitFor();
		assert.match(await status.innerText(), /模型未返回符合 JSON 契约/);
		assert.match(await status.innerText(), /Expecting ',' delimiter/);
		assert.match(await status.innerText(), /已保存 7\/13 页/);
		assert.equal(await status.getByRole('button', { name: '重新开始任务' }).count(), 1);
		const failedPosition = await status.boundingBox();
		assert.ok(
			failedPosition.y >= 0 && failedPosition.y + failedPosition.height <= 720,
			'Failure and recovery action must stay visible',
		);
		await page.setViewportSize({ width: 800, height: 600 });
		await status.getByRole('button', { name: '查看详情', exact: true }).click();
		const recovery = status.getByRole('button', { name: '从检查点继续', exact: true });
		assert.ok(
			await recovery.evaluate((element) => {
				const box = element.getBoundingClientRect();
				const hit = document.elementFromPoint(
					box.x + box.width / 2,
					box.y + box.height / 2,
				);
				return element === hit || element.contains(hit);
			}),
			'Narrow desktop preview must not cover the recovery button',
		);
		const narrowBox = await recovery.boundingBox();
		assert.ok(narrowBox.y + narrowBox.height <= 600, 'Recovery remains in the narrow viewport');
		assert.deepEqual(
			mutations,
			[],
			'Observing a failed job must never resume or retry it automatically',
		);
		await page.screenshot({ path: '/tmp/pptd-visible-failure-regression.png' });
	} finally {
		await browser.close();
	}
});
