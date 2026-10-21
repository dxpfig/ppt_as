const assert = require('node:assert/strict');
const { test } = require('node:test');
const { chromium } = require('playwright');

const projectId = 'page-actions-project';
const pagePath = 'pages/one.page';
const page = {
	path: pagePath,
	data: {
		elements: [
			{
				elementId: 'title',
				elementType: 'text',
				bounds: [80, 80, 800, 80],
				content: { text: '当前页面', fontSize: 32 },
			},
		],
	},
};

function project() {
	return {
		id: projectId,
		title: '页面操作测试',
		theme: 'forest',
		phase: 'ready',
		slides: [],
		messages: [],
		versions: [],
		contentGenerated: true,
		workflow: 'pptd-v2',
		pptd: {
			entry: 'document.pptd',
			revision: 2,
			pageCount: 1,
			viewUrl: `/api/studio/projects/${projectId}/pptd/view?embed=true`,
			sourceUrl: `/api/studio/projects/${projectId}/pptd/source.zip`,
			versions: [],
		},
		artifact: null,
	};
}

test('page actions target the selected page and open a design dialog', async () => {
	const browser = await chromium.launch({
		executablePath:
			process.env.PPTD_CHROMIUM || process.env.CHROMIUM_PATH || '/snap/bin/chromium',
		headless: true,
		args: ['--no-sandbox'],
	});
	const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
	const requests = [];
	await context.route('**/api/studio/**', async (route) => {
		const request = route.request();
		const url = new URL(request.url());
		const path = url.pathname.replace('/api/studio', '');
		const method = request.method();
		const body = request.postDataJSON?.();
		if (path === '/health')
			return route.fulfill({
				json: {
					status: 'ok',
					llmConfigured: true,
					model: 'fixture',
					provider: 'openai',
					rendererAvailable: true,
				},
			});
		if (path === '/projects') return route.fulfill({ json: [] });
		if (path === '/templates') return route.fulfill({ json: [] });
		if (path === `/projects/${projectId}/reference-files`) return route.fulfill({ json: [] });
		if (path === '/reference-budget')
			return route.fulfill({
				json: {
					available: true,
					provider: 'openai',
					model: 'fixture',
					contextTokens: 128000,
					outputTokens: 16000,
					safetyTokens: 4000,
					inputTokens: 108000,
					charactersPerToken: 2,
					maxCharacters: 216000,
					maxFileBytes: 52428800,
					source: 'fixture',
					counting: 'fixture',
				},
			});
		if (path === `/projects/${projectId}` && method === 'GET')
			return route.fulfill({ json: project() });
		if (path === `/projects/${projectId}/pptd`)
			return route.fulfill({
				json: {
					document: {
						version: 'v2',
						title: '页面操作测试',
						size: [960, 540],
						pages: [pagePath],
					},
					pages: [page],
					revision: 2,
					versions: [],
				},
			});
		if (path === `/projects/${projectId}/pptd/view`)
			return route.fulfill({
				contentType: 'text/html',
				body: `<!doctype html><html><body><h1>当前页面</h1><script>
					addEventListener('message', event => {
						if (event.data?.type === 'pptd:select-page') document.body.dataset.page = event.data.page;
					});
					parent.postMessage({type:'pptd:ready', projectId:'${projectId}'}, location.origin);
				</script></body></html>`,
			});
		if (path === '/jobs' && method === 'POST') {
			requests.push(body);
			return route.fulfill({
				status: 202,
				json: {
					id: `job-${requests.length}`,
					projectId,
					kind: body.kind,
					status: 'running',
					stage: 5,
					message: '正在处理当前页面',
				},
			});
		}
		if (path.startsWith('/jobs/') && method === 'GET')
			return route.fulfill({
				json: {
					id: path.split('/')[2],
					projectId,
					kind: requests.at(-1)?.kind,
					status: 'running',
					stage: 5,
					message: '正在处理当前页面',
				},
			});
		return route.fulfill({ status: 404, json: { detail: 'fixture route missing' } });
	});

	const tab = await context.newPage();
	await tab.goto((process.env.PPTD_UI_URL || 'http://127.0.0.1:5174') + `/?project=${projectId}`);
	await tab.getByRole('button', { name: '页面修复', exact: true }).waitFor();
	await tab.getByRole('button', { name: 'AI 视觉检视', exact: true }).waitFor();
	await tab.getByRole('button', { name: '设计', exact: true }).click();
	await tab
		.getByPlaceholder('例如：改成左右双栏，左侧放核心结论，右侧用三步流程表达实施路径。')
		.fill('改成左右双栏并突出核心结论');
	await tab.getByRole('button', { name: '生成设计稿', exact: true }).click();
	await tab.locator('.pptd-page-action-progress').waitFor();
	assert.equal(requests[0].kind, 'page-design');
	assert.equal(requests[0].selectedSlideId, pagePath);
	assert.match(requests[0].prompt, /左右双栏/);

	await tab.evaluate(() => localStorage.clear());
	await tab.reload();
	await tab.getByRole('button', { name: 'AI 视觉检视', exact: true }).click();
	await tab.locator('.pptd-page-action-progress').waitFor();
	assert.equal(requests[1].kind, 'page-review');
	assert.equal(requests[1].selectedSlideId, pagePath);

	await context.close();
	await browser.close();
});
