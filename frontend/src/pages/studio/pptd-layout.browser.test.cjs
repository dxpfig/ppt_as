const assert = require('node:assert/strict');
const { test } = require('node:test');
const { chromium } = require('playwright');

const projectId = 'responsive-layout-project';
const pages = Array.from({ length: 13 }, (_, index) => ({
	path: `pages/${String(index + 1).padStart(2, '0')}.page`,
	data: {
		elements: [
			{
				elementId: 'title',
				elementType: 'text',
				content: { text: `<p>第 ${index + 1} 页：数字化平台建设与实施规划</p>` },
			},
		],
	},
}));
const project = {
	id: projectId,
	title: '公司数字化平台 AI 大模型建设方案与年度实施计划',
	workflow: 'pptd-v2',
	phase: 'ready',
	theme: 'forest',
	slides: [],
	messages: [],
	versions: [],
	pptd: {
		revision: 9,
		pageCount: pages.length,
		viewUrl: `/api/studio/projects/${projectId}/pptd/view`,
		sourceUrl: `/api/studio/projects/${projectId}/pptd/source.zip`,
		versions: [],
	},
	artifact: {
		id: 'layout-artifact',
		revision: 9,
		pptxUrl: '/api/studio/artifacts/layout-artifact/presentation.pptx',
		pdfUrl: '/api/studio/artifacts/layout-artifact/presentation.pdf',
		previewUrls: pages.map(
			(_, index) =>
				'data:image/svg+xml,' +
				encodeURIComponent(
					`<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180"><rect width="320" height="180" fill="#eef3e8"/><text x="160" y="95" text-anchor="middle" font-size="28" fill="#244b3c">${index + 1}</text></svg>`,
				),
		),
		warnings: Array.from(
			{ length: 12 },
			(_, index) => `导出说明 ${index + 1}：字体已嵌入演示文件。`,
		),
	},
};

async function openWorkspace(t, viewport) {
	const browser = await chromium.launch({
		executablePath: process.env.CHROMIUM_PATH || '/snap/bin/chromium',
		headless: true,
		args: ['--no-sandbox'],
	});
	t.after(() => browser.close());
	const page = await browser.newPage({ viewport });
	page.setDefaultTimeout(8000);
	const errors = [];
	const mutations = [];
	page.on('pageerror', (error) => errors.push(error.message));
	await page.route('**/api/studio/**', async (route) => {
		const request = route.request();
		const path = new URL(request.url()).pathname.replace('/api/studio', '');
		if (!['GET', 'HEAD'].includes(request.method())) {
			mutations.push(`${request.method()} ${path}`);
			return route.fulfill({ status: 405, body: 'Read-only layout fixture' });
		}
		const json = (value) =>
			route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) });
		if (path === '/health')
			return json({ llmConfigured: true, model: 'Layout fixture', rendererAvailable: true });
		if (path === '/templates') return json([]);
		if (path === '/projects')
			return json([{ id: projectId, title: project.title, slideCount: 13 }]);
		if (path === `/projects/${projectId}`) return json(project);
		if (path === `/projects/${projectId}/pptd`)
			return json({ document: { title: project.title }, pages, revision: 9, versions: [] });
		if (path === `/projects/${projectId}/pptd/view`)
			return route.fulfill({
				contentType: 'text/html',
				body: `<!doctype html><html><head><style>html,body{margin:0;width:100%;height:100%;overflow:hidden}body{display:grid;place-items:center;background:#e9eddf;font-family:sans-serif}main{background:white;width:90%;aspect-ratio:16/9;display:grid;place-items:center}</style></head><body><main>文稿预览</main><script>
				const pagePaths = ${JSON.stringify(pages.map((item) => item.path))};
				const select = (path) => {
					document.body.dataset.page = path;
					document.querySelector('main').textContent = path;
				};
				addEventListener('message', (event) => {
					if (event.data.type === 'pptd:select-page') {
						select(event.data.page);
					}
					if (event.data.type === 'pptd:navigate') {
						const index = pagePaths.indexOf(document.body.dataset.page);
						const path = pagePaths[Math.min(pagePaths.length - 1, Math.max(0, index + event.data.direction))];
						select(path);
						parent.postMessage({type:'pptd:page-selected',projectId:'${projectId}',page:path},location.origin);
					}
				});
				addEventListener('keydown', (event) => {
					if(event.key === 'Escape') parent.postMessage({type:'pptd:exit-presentation',projectId:'${projectId}'},location.origin);
				});
				parent.postMessage({type:'pptd:ready',projectId:'${projectId}'},location.origin);
				</script></body></html>`,
			});
		return route.fulfill({ status: 404, body: 'Unknown layout fixture route' });
	});
	t.after(() => {
		assert.deepEqual(errors, [], 'Workspace has no browser errors');
		assert.deepEqual(
			mutations,
			[],
			'Layout interactions never generate, edit or export a project',
		);
	});
	await page.goto(
		(process.env.PPTD_UI_URL || 'http://127.0.0.1:5174') + `/?project=${projectId}`,
	);
	await page.locator('.pptd-page-list button').last().waitFor();
	await page.waitForFunction(
		() =>
			document.querySelector('iframe')?.contentDocument?.body.dataset.page ===
			'pages/01.page',
	);
	return page;
}

async function assertNoHorizontalOverflow(page, width) {
	const sizes = await page.evaluate(() => ({
		document: document.documentElement.scrollWidth,
		body: document.body.scrollWidth,
	}));
	assert.ok(sizes.document <= width, `Document ${sizes.document}px fits ${width}px viewport`);
	assert.ok(sizes.body <= width, `Body ${sizes.body}px fits ${width}px viewport`);
}

async function assertCurrentPage(page, index) {
	await page.waitForFunction(
		(path) => document.querySelector('iframe')?.contentDocument?.body.dataset.page === path,
		pages[index].path,
	);
	assert.match(
		await page.locator('.pptd-page-list button[aria-current="page"]').innerText(),
		new RegExp(`第 ${index + 1} 页`),
	);
}

test('1366×768 has a vertical page rail, usable preview and compact export bar', async (t) => {
	const page = await openWorkspace(t, { width: 1366, height: 768 });
	const nav = await page.locator('.pptd-page-list').boundingBox();
	const frame = await page.locator('iframe').boundingBox();
	const first = await page.locator('.pptd-page-list button').nth(0).boundingBox();
	const second = await page.locator('.pptd-page-list button').nth(1).boundingBox();
	assert.ok(
		Math.abs(first.x - second.x) <= 1 && second.y > first.y,
		'Pages are listed vertically',
	);
	assert.ok(nav.x + nav.width <= frame.x + 1, 'Page rail sits beside the preview');
	assert.ok(
		frame.width >= 600 && frame.height >= 400,
		'Notebook preview has practical reading space',
	);
	assert.ok(frame.y >= 0 && frame.y + frame.height <= 768, 'Preview fits the viewport');
	const footer = await page.locator('.pptd-export').boundingBox();
	assert.ok(footer.height <= 52, `Export bar is compact, received ${footer.height}px`);
	assert.ok(footer.y + footer.height <= 768, 'Export controls remain visible');
	const warnings = page.locator('.pptd-export-warnings');
	assert.equal(await warnings.evaluate((element) => element.tagName), 'DETAILS');
	assert.equal(
		await warnings.getAttribute('open'),
		null,
		'Export explanations are collapsed initially',
	);
	await page.getByRole('button', { name: '全屏播放', exact: true }).waitFor();
	await assertNoHorizontalOverflow(page, 1366);
});

test('page rail wheel changes one page per gesture and synchronizes the preview', async (t) => {
	const page = await openWorkspace(t, { width: 1366, height: 768 });
	const rail = page.locator('.pptd-page-list');
	await rail.hover();
	for (let index = 0; index < 5; index++) await page.mouse.wheel(0, 100);
	await assertCurrentPage(page, 1);
	await page.waitForTimeout(650);
	await page.mouse.wheel(0, 100);
	await assertCurrentPage(page, 2);
	await page.waitForTimeout(650);
	await page.mouse.wheel(0, -100);
	await assertCurrentPage(page, 1);
	await rail.locator('button').last().click();
	await assertCurrentPage(page, 12);
	await page.waitForTimeout(650);
	await rail.hover();
	await page.mouse.wheel(0, 100);
	await assertCurrentPage(page, 12);
});

test('fullscreen presents the current slide, navigates, and restores the workspace', async (t) => {
	const page = await openWorkspace(t, { width: 1366, height: 768 });
	await page.locator('.pptd-page-list button').nth(3).click();
	await assertCurrentPage(page, 3);
	await page.getByRole('button', { name: '全屏播放', exact: true }).click();
	await page.locator('.pptd-workbench.is-presenting').waitFor();
	const frame = await page.locator('iframe').boundingBox();
	assert.ok(frame.x <= 1 && frame.y <= 1, 'Presentation starts at the viewport origin');
	assert.ok(frame.width >= 1365 && frame.height >= 700, 'Presentation fills the viewport');
	await page.getByRole('button', { name: '下一页', exact: true }).click();
	await assertCurrentPage(page, 4);
	await page.getByRole('button', { name: '上一页', exact: true }).click();
	await assertCurrentPage(page, 3);
	await page.getByRole('button', { name: '退出全屏', exact: true }).click();
	await page.waitForFunction(() => !document.querySelector('.pptd-workbench.is-presenting'));
	await page.waitForFunction(() => !document.fullscreenElement);
	assert.equal(
		await page.getByRole('button', { name: '全屏播放', exact: true }).isVisible(),
		true,
	);
	await assertCurrentPage(page, 3);
	await page.getByRole('button', { name: '全屏播放', exact: true }).click();
	await page.locator('.pptd-workbench.is-presenting').waitFor();
	await page.keyboard.press('Escape');
	await page.waitForFunction(() => !document.querySelector('.pptd-workbench.is-presenting'));
	await assertNoHorizontalOverflow(page, 1366);
});

for (const viewport of [
	{ width: 1024, height: 768 },
	{ width: 390, height: 844 },
]) {
	test(`${viewport.width}×${viewport.height} keeps preview and settings usable`, async (t) => {
		const page = await openWorkspace(t, viewport);
		await assertNoHorizontalOverflow(page, viewport.width);
		const frame = await page.locator('iframe').boundingBox();
		assert.ok(
			frame.width >= Math.min(240, viewport.width * 0.6),
			'Preview retains usable width',
		);
		assert.ok(frame.height >= 180, 'Preview retains usable height');
		assert.ok(
			frame.y >= 0 && frame.y + frame.height <= viewport.height,
			'Preview fits the screen',
		);
		const settings = page.getByRole('button', { name: '创作设置', exact: true });
		await settings.waitFor();
		if ((await settings.getAttribute('aria-expanded')) !== 'true') await settings.click();
		const prompt = page.getByLabel('修改要求');
		await prompt.waitFor();
		await prompt.fill('只输入布局验收文字，不提交生成');
		assert.equal(await prompt.inputValue(), '只输入布局验收文字，不提交生成');
		await assertNoHorizontalOverflow(page, viewport.width);
	});
}
