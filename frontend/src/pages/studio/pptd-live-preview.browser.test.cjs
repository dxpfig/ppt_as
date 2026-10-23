const assert = require('node:assert/strict');
const { test } = require('node:test');
const { chromium } = require('playwright');

test('live page preview shows actual image copy and per-page review status', async () => {
	const browser = await chromium.launch({
		executablePath: process.env.CHROMIUM_PATH || '/snap/bin/chromium',
		headless: true,
		args: ['--no-sandbox'],
	});
	const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
	const now = Date.now() / 1000;
	const project = {
		id: 'live-preview-project',
		title: '实时预览',
		slides: [],
		messages: [],
		versions: [],
		theme: 'forest',
		phase: 'generating',
		workflow: 'pptd-v2',
		activeJobId: 'live-preview-job',
	};
	const job = {
		id: 'live-preview-job',
		kind: 'kimi-slides',
		status: 'running',
		stage: 45,
		message: '正在生成第 2 页',
		createdAt: now - 20,
		updatedAt: now,
		draftPreview: {
			status: 'draft',
			completedPages: 1,
			reviewedPages: 1,
			issuePages: 0,
			totalPages: 3,
			latestIndex: 0,
			pages: [
				{
					path: 'pages/1.page',
					index: 0,
					title: '第一页结论',
					status: 'ready',
					previewUrl: '/api/studio/jobs/live-preview-job/draft-previews/page-1.png',
					text: '这是与预览同步生成的页面正文。',
					reviewStatus: 'passed',
					issueCount: 0,
					issues: [],
				},
				{
					path: 'pages/2.page',
					index: 1,
					title: '第二页',
					status: 'ready',
					previewUrl: '/api/studio/jobs/live-preview-job/draft-previews/page-2.png',
					text: '这一页仍有一项版式问题。',
					reviewStatus: 'issues',
					issueCount: 1,
					issues: [
						{
							severity: 'error',
							code: 'text-overflow',
							message: '文本超出可用空间',
						},
					],
				},
				{
					path: 'pages/3.page',
					index: 2,
					title: '第三页',
					status: 'pending',
					previewUrl: null,
					text: '',
					reviewStatus: 'pending',
					issueCount: 0,
					issues: [],
				},
			],
		},
	};

	await page.route('**/api/studio/**', async (route) => {
		const request = route.request();
		const path = new URL(request.url()).pathname.replace('/api/studio', '');
		const json = (value) =>
			route.fulfill({
				contentType: 'application/json',
				body: JSON.stringify(value),
			});
		if (path === '/health')
			return json({
				llmConfigured: true,
				model: 'Configured model',
				rendererAvailable: true,
			});
		if (path === '/templates' || path === '/projects') return json([]);
		if (path === '/projects/live-preview-project') return json(project);
		if (path === '/jobs/live-preview-job') return json(job);
		if (path.endsWith('/events')) return route.fulfill({ status: 404, body: 'poll' });
		if (path.endsWith('/draft-previews/page-1.png'))
			return route.fulfill({
				contentType: 'image/png',
				body: Buffer.from(
					'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=',
					'base64',
				),
			});
		if (path.endsWith('/draft-previews/page-2.png'))
			return route.fulfill({
				contentType: 'image/png',
				body: Buffer.from(
					'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=',
					'base64',
				),
			});
		return route.fulfill({ status: 404, body: 'unhandled' });
	});

	try {
		await page.goto(
			(process.env.PPTD_UI_URL || 'http://127.0.0.1:5174') + '/?project=live-preview-project',
		);
		const preview = page.locator('.pptd-live-draft');
		await preview.waitFor({ timeout: 10000 });
		assert.match(await preview.innerText(), /逐页实际预览/);
		assert.match(await preview.innerText(), /1 页检视通过/);
		assert.match(await preview.innerText(), /第二页/);
		assert.match(await preview.innerText(), /发现 1 项问题/);
		assert.equal(await preview.locator('.pptd-live-draft-stage img').count(), 1);
		const issueCopy = preview.locator('.pptd-live-draft-copy.is-issues');
		assert.equal(
			await issueCopy.evaluate((element) => getComputedStyle(element).position),
			'fixed',
		);
		assert.equal(await issueCopy.locator('p').isVisible(), false);
		await issueCopy.locator('summary').click();
		assert.match(await issueCopy.innerText(), /这一页仍有一项版式问题/);
		assert.match(await issueCopy.innerText(), /文本超出可用空间/);

		await preview.locator('.pptd-live-draft-pages button').first().click();
		const passedCopy = preview.locator('.pptd-live-draft-copy.is-passed');
		await passedCopy.waitFor();
		assert.equal(
			await passedCopy.evaluate((element) => getComputedStyle(element).position),
			'fixed',
		);
		assert.equal(await passedCopy.locator('p').isVisible(), false);
		const box = await passedCopy.boundingBox();
		const viewport = page.viewportSize();
		assert.ok(box && viewport);
		assert.ok(box.x + box.width <= viewport.width);
		assert.ok(box.y + box.height <= viewport.height);
		assert.ok(box.x + box.width > viewport.width - 80);
		assert.ok(box.y + box.height > viewport.height - 120);
		await passedCopy.locator('summary').click();
		assert.match(await passedCopy.innerText(), /这是与预览同步生成的页面正文/);
	} finally {
		await browser.close();
	}
});
