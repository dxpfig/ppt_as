const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { test } = require('node:test');
const { chromium } = require('playwright');

const mammothRequire = createRequire(require.resolve('mammoth'));
const JSZip = mammothRequire('jszip');

async function referenceDocx(text) {
	const zip = new JSZip();
	zip.file(
		'[Content_Types].xml',
		'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
	);
	zip.file(
		'_rels/.rels',
		'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
	);
	zip.file(
		'word/document.xml',
		`<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>${text}</w:t></w:r></w:p><w:sectPr/></w:body></w:document>`,
	);
	return zip.generateAsync({ type: 'nodebuffer' });
}

// Run with the frontend dev server at PPTD_UI_URL (default http://127.0.0.1:5178).
// Service responses are controlled here; real renderer/export acceptance is separate.
test('main workspace uses authoritative documents, revisions, recovery, and real template import', async () => {
	const browser = await chromium.launch({
		executablePath: process.env.CHROMIUM_PATH || '/snap/bin/chromium',
		headless: true,
		args: ['--no-sandbox'],
	});
	const page = await browser.newPage({ viewport: { width: 1600, height: 1050 } });
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.message));
	const projects = new Map();
	const referenceFiles = new Map();
	const requests = [];
	const jobs = new Map();
	let jobNumber = 0;
	let referenceNumber = 0;
	const version = (revision) => ({
		id: 'v' + revision,
		revision,
		label: '版本 ' + revision,
		createdAt: '2026-10-09T12:00:00Z',
		pageCount: 2,
	});
	function publish(project, revision = 1) {
		return {
			...project,
			workflow: 'pptd-v2',
			title: '验收演示',
			slides: [],
			activeJobId: null,
			phase: 'ready',
			pptd: {
				revision,
				pageCount: 2,
				viewUrl: '/api/studio/projects/' + project.id + '/pptd/view',
				sourceUrl: '/api/studio/projects/' + project.id + '/pptd/source.zip',
				versions: Array.from({ length: revision }, (_, i) => version(i + 1)),
			},
		};
	}
	const pages = [
		{
			path: 'pages/intro.pptd',
			data: {
				elements: [
					{ elementId: 'title', elementType: 'text', content: { text: '<p>开场</p>' } },
				],
			},
		},
		{
			path: 'pages/results.pptd',
			data: {
				elements: [
					{
						elementId: 'title',
						elementType: 'text',
						content: { text: '<p><strong>核心结果</strong></p>' },
					},
				],
			},
		},
	];
	await page.route('**/api/studio/**', async (route) => {
		const request = route.request();
		const path = new URL(request.url()).pathname.replace('/api/studio', '');
		const method = request.method();
		let body = null;
		try {
			body = request.postDataJSON();
		} catch {
			/* Multipart requests have no JSON body. */
		}
		requests.push({ path, method, body });
		const json = (value, status = 200) =>
			route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
		if (path === '/health')
			return json({
				llmConfigured: true,
				model: 'Configured live model',
				provider: 'openai',
				rendererAvailable: true,
			});
		if (path === '/templates')
			return json([
				{ id: 'corporate', name: '公司模板', slideCount: 2, layouts: [], warnings: [] },
			]);
		if (path === '/reference-budget')
			return json({
				available: true,
				provider: 'openai',
				model: 'Configured live model',
				contextTokens: 128000,
				outputTokens: 32000,
				safetyTokens: 4000,
				inputTokens: 92000,
				charactersPerToken: 2,
				maxCharacters: 184000,
				maxFileBytes: 50 * 1024 * 1024,
				source: 'model-api',
				counting: 'responses-input-tokens',
			});
		const referenceMatch = path.match(
			/^\/projects\/([^/]+)\/reference-files(?:\/([^/]+)(?:\/(text|download))?)?$/,
		);
		if (referenceMatch) {
			const [, projectId, attachmentId, action] = referenceMatch;
			const records = referenceFiles.get(projectId) ?? [];
			if (method === 'POST') {
				referenceNumber += 1;
				const id = 'reference-' + referenceNumber;
				const base = '/api/studio/projects/' + projectId + '/reference-files/' + id;
				const attachment = {
					id,
					name: '三季度复盘.docx',
					size: 1024,
					extension: '.docx',
					characters: 31,
					createdAt: Date.now(),
					downloadUrl: base + '/download',
					textUrl: base + '/text',
				};
				referenceFiles.set(projectId, [attachment, ...records]);
				return json(attachment, 201);
			}
			if (method === 'DELETE') {
				referenceFiles.set(
					projectId,
					records.filter((item) => item.id !== attachmentId),
				);
				return json({ id: attachmentId, deleted: true });
			}
			if (action === 'text')
				return json({
					...records.find((item) => item.id === attachmentId),
					text: '附件关键数据：项目完成率 83%。下一步重点是续约与交付风险。',
				});
			if (action === 'download')
				return route.fulfill({
					contentType:
						'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
					body: 'WORD',
				});
			return json(records);
		}
		if (path === '/projects' && method === 'GET')
			return json(
				[...projects.values()].map((item) => ({
					id: item.id,
					title: item.title,
					slideCount: item.pptd?.pageCount || item.slides.length,
				})),
			);
		if (path === '/jobs' && method === 'POST') {
			const id = 'job-' + ++jobNumber;
			let saved =
				body.kind === 'render'
					? {
							...projects.get(body.project.id),
							artifact: {
								id: 'artifact-' + id,
								revision: body.project.pptd.revision,
								pptxUrl: '/artifact.pptx',
								pdfUrl: '/artifact.pdf',
								previewUrls: [],
								warnings: [],
							},
						}
					: publish(
							body.project,
							(projects.get(body.project.id)?.pptd?.revision || 0) + 1,
						);
			saved.lastJobId = id;
			projects.set(saved.id, saved);
			jobs.set(id, {
				id,
				status: 'completed',
				stage: 100,
				message: '完成',
				result: { project: saved },
			});
			return json({ id, status: 'queued', stage: 0, message: '排队中' });
		}
		if (path === '/jobs/job-1/events')
			return route.fulfill({
				contentType: 'text/event-stream',
				body: 'data: ' + JSON.stringify(jobs.get('job-1')) + '\n\n',
			});
		const jobMatch = path.match(/^\/jobs\/([^/]+)(?:\/(resume|retry|cancel))?$/);
		if (jobMatch) {
			const job = jobs.get(jobMatch[1]);
			if (jobMatch[2] === 'resume' || jobMatch[2] === 'retry') {
				const saved = projects.get(job.projectId);
				jobs.set(job.id, {
					id: job.id,
					status: 'completed',
					stage: 100,
					message: '完成',
					result: { project: saved },
				});
				return json({ id: job.id, status: 'running', stage: 40, message: '恢复中' });
			}
			return json(job);
		}
		const match = path.match(/^\/projects\/([^/]+)(.*)$/);
		if (match) {
			const [, id, suffix] = match;
			let project = projects.get(id);
			if (!suffix && method === 'PUT') {
				projects.set(id, body);
				return json(body);
			}
			if (!project) return json({ detail: 'not found' }, 404);
			if (!suffix) return json(project);
			if (suffix === '/pptd')
				return json({
					document: { title: project.title },
					pages,
					revision: project.pptd.revision,
					versions: project.pptd.versions,
				});
			if (suffix === '/pptd/view')
				return route.fulfill({
					contentType: 'text/html',
					body:
						'<!doctype html><html><body style="font-family:sans-serif;padding:50px;background:#eef2e6"><h1>真实文档预览</h1><button id="edit">保存元素修改</button><script>addEventListener("message",event=>{if(event.data.type==="pptd:select-page")document.body.dataset.page=event.data.page});document.querySelector("#edit").onclick=async()=>{await fetch("/api/studio/projects/' +
						id +
						'/test-edit",{method:"POST"});parent.postMessage({type:"pptd:changed",projectId:"' +
						id +
						'"},location.origin)};parent.postMessage({type:"pptd:ready",projectId:"' +
						id +
						'"},location.origin)</script></body></html>',
				});
			if (suffix === '/test-edit' || suffix.startsWith('/pptd/versions/')) {
				project = publish({ ...project, artifact: undefined }, project.pptd.revision + 1);
				projects.set(id, project);
				return json(project);
			}
			if (suffix === '/pptd/migrate') {
				project = publish({ ...project, artifact: undefined });
				projects.set(id, project);
				return json(project);
			}
			if (suffix === '/pptd/import-template') {
				// The template is analyzed into a reference; it never becomes the
				// project document, so no revision is published by this call.
				project = {
					...project,
					templateId: body.templateId,
					templateReference: {
						kind: 'company-template-reference',
						pageCount: 12,
						pages: [],
					},
				};
				projects.set(id, project);
				return json(project);
			}
		}
		return json({ detail: 'Unexpected route ' + method + ' ' + path }, 404);
	});
	try {
		await page.goto(process.env.PPTD_UI_URL || 'http://127.0.0.1:5178');
		await page.getByText('Configured live model', { exact: true }).waitFor();
		await page.getByText('参考材料与内容选项', { exact: true }).click();
		await page.locator('input[aria-label^="参考文本文件"]').setInputFiles({
			name: '三季度复盘.docx',
			mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
			buffer: await referenceDocx(
				'附件关键数据：项目完成率 83%。下一步重点是续约与交付风险。',
			),
		});
		await page.getByText('三季度复盘.docx', { exact: true }).waitFor();
		await page.getByText(/已添加 ·/).waitFor();
		await page.getByRole('button', { name: '查看参考资料 三季度复盘.docx' }).click();
		await page.getByText('已解析正文预览', { exact: true }).waitFor();
		const download = page.getByRole('link', { name: '下载原文件' });
		assert.match(
			await download.getAttribute('href'),
			/\/reference-files\/reference-1\/download$/,
		);
		await page.getByRole('button', { name: '关闭参考资料预览' }).click();
		await page.getByLabel('演示主题或内容').fill('给团队讲解季度业务结果');
		await page.getByRole('button', { name: '生成演示', exact: true }).click();
		await page.locator('iframe[title="演示文稿预览与编辑器"]').waitFor();
		await page.getByRole('button', { name: /02\s*核心结果/ }).waitFor();
		const generated = requests.find((item) => item.path === '/jobs' && item.method === 'POST');
		assert.equal(generated.body.pageCount, null);
		assert.match(generated.body.references, /项目完成率 83%/);
		assert.match(generated.body.references, /续约与交付风险/);
		assert.ok(
			requests.some(
				(item) =>
					item.path === `/projects/${generated.body.project.id}` &&
					item.method === 'PUT' &&
					/项目完成率 83%/.test(item.body.references),
			),
			'Parsed attachment text is persisted before generation',
		);
		assert.equal(generated.body.project.workflow, 'pptd-v2');
		assert.equal(generated.body.workflow, 'new');
		assert.equal(generated.body.speakerNotes, false);
		assert.ok(
			requests.some((item) => item.path === '/jobs/job-1/events'),
			'Generation receives server-sent job events',
		);
		assert.equal(await page.locator('.slide-draft').count(), 0);
		await page
			.frameLocator('iframe[title="演示文稿预览与编辑器"]')
			.getByRole('button', { name: '保存元素修改' })
			.waitFor();
		await page.getByRole('button', { name: /02\s*核心结果/ }).click();
		await page.waitForFunction(
			() =>
				document.querySelector('iframe[title="演示文稿预览与编辑器"]').contentDocument.body
					.dataset.page === 'pages/results.pptd',
		);
		await page.getByRole('button', { name: '导出 PPTX / PDF', exact: true }).click();
		await page.getByRole('link', { name: 'PPTX', exact: true }).waitFor();
		await page
			.frameLocator('iframe[title="演示文稿预览与编辑器"]')
			.getByRole('button', { name: '保存元素修改' })
			.click();
		await page.getByRole('button', { name: '导出 PPTX / PDF', exact: true }).waitFor();
		assert.equal(await page.getByRole('link', { name: 'PPTX', exact: true }).count(), 0);
		await page.waitForFunction(
			() =>
				document.querySelector('iframe[title="演示文稿预览与编辑器"]').contentDocument.body
					.dataset.page === 'pages/results.pptd',
		);
		await page.getByRole('button', { name: '版本记录', exact: true }).click();
		await page.getByRole('button', { name: '恢复', exact: true }).first().click();
		await page.getByText(/已保存 · 版本 \d+/).waitFor();
		assert.equal(
			requests.find((item) => item.path.includes('/versions/') && item.method === 'POST').body
				.expectedRevision,
			2,
		);
		await page.getByRole('button', { name: '关闭版本记录' }).click();
		await page.getByLabel('修改要求').fill('突出当前页面核心结论');
		await page.getByRole('button', { name: '应用修改', exact: true }).click();
		await page.getByText('已保存 · 版本 4', { exact: true }).waitFor();
		const revised = requests
			.filter((item) => item.path === '/jobs' && item.method === 'POST')
			.at(-1);
		assert.equal(revised.body.kind, 'revise');
		assert.ok(
			requests.some((item) => item.path === '/jobs/job-2'),
			'Polling recovers when the event stream is unavailable',
		);
		assert.equal(revised.body.selectedSlideId, 'pages/results.pptd');
		const current = [...projects.values()][0];
		current.lastJobId = 'interrupted';
		jobs.set('interrupted', {
			id: 'interrupted',
			projectId: current.id,
			status: 'interrupted',
			recoverable: true,
			stage: 45,
			message: '连接中断',
			checkpoint: { summary: '已完成内容规划' },
		});
		await page.reload();
		await page
			.locator('.pptd-workbench')
			.getByRole('button', { name: '从检查点继续', exact: true })
			.click();
		await page.getByText('已保存 · 版本 4', { exact: true }).waitFor();
		assert.ok(requests.some((item) => item.path === '/jobs/interrupted/resume'));
		const legacy = {
			id: 'legacy',
			title: '旧版演示',
			theme: 'forest',
			phase: 'ready',
			slides: [{ id: 'old', title: '旧版内容', subtitle: '', points: ['观点'], notes: '' }],
			versions: [],
			messages: [],
		};
		projects.set('legacy', legacy);
		await page.getByRole('button', { name: '刷新工作记录' }).click();
		await page.getByRole('button', { name: '打开工作记录：旧版演示' }).click();
		await page.getByRole('button', { name: '迁移为可编辑演示' }).click();
		await page.locator('iframe[title="演示文稿预览与编辑器"]').waitFor();
		assert.ok(requests.some((item) => item.path === '/projects/legacy/pptd/migrate'));
		await page.getByRole('button', { name: '新建', exact: true }).click();
		await page.getByLabel('视觉设计').selectOption('style-reference');
		await page.locator('input[accept="image/png,image/jpeg,image/webp"]').setInputFiles({
			name: 'reference.png',
			mimeType: 'image/png',
			buffer: Buffer.from(
				'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==',
				'base64',
			),
		});
		await page.getByRole('button', { name: '移除参考图 reference.png' }).waitFor();
		await page.getByLabel('视觉设计').selectOption('template');
		await page.locator('.pptd-template select').selectOption('corporate');
		await page.getByLabel('演示主题或内容').fill('使用上传模板生成产品介绍');
		await page.getByRole('button', { name: '生成演示', exact: true }).click();
		await page.locator('iframe[title="演示文稿预览与编辑器"]').waitFor();
		const imported = requests.find((item) => item.path.endsWith('/pptd/import-template'));
		assert.equal(imported.body.templateId, 'corporate');
		const templateRequest = requests
			.filter((item) => item.path === '/jobs' && item.method === 'POST')
			.at(-1);
		// The template only contributes a reference; the deck starts empty and the
		// generated pages are the user's own.
		assert.equal(templateRequest.body.project.templateReference.pageCount, 12);
		assert.equal(templateRequest.body.project.pptd, undefined);
		assert.equal(templateRequest.body.attachments[0].name, 'reference.png');
		assert.match(templateRequest.body.attachments[0].imageData, /^data:image\/png;base64,/);
		assert.equal(templateRequest.body.expectedRevision, undefined);
		assert.ok(requests.indexOf(imported) < requests.indexOf(templateRequest));
		await page.evaluate(() => {
			const cached = JSON.parse(localStorage.getItem('ppt-as-studio-v3'));
			delete cached.pptd;
			localStorage.setItem('ppt-as-studio-v3', JSON.stringify(cached));
		});
		await page.reload();
		await page.locator('iframe[title="演示文稿预览与编辑器"]').waitFor();
		assert.equal(
			await page.getByLabel('创作方式').inputValue(),
			'edit',
			'Hydrated documents default to editing',
		);
		await page.getByLabel('创作方式').selectOption('new');
		await page
			.frameLocator('iframe[title="演示文稿预览与编辑器"]')
			.getByRole('button', { name: '保存元素修改' })
			.click();
		await page.getByText(/已保存 · 版本 \d+/).waitFor();
		assert.equal(
			await page.getByLabel('创作方式').inputValue(),
			'new',
			'Manual workflow selection survives document updates',
		);
		await page.screenshot({ path: '/tmp/pptd-studio-desktop.png', fullPage: true });
		await page.setViewportSize({ width: 390, height: 844 });
		await page.screenshot({ path: '/tmp/pptd-studio-mobile.png', fullPage: true });
		assert.ok(
			await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
			'Mobile workspace must not overflow horizontally',
		);
		assert.deepEqual(errors, []);
	} catch (error) {
		await page.screenshot({ path: '/tmp/pptd-studio-failure.png', fullPage: true });
		throw error;
	} finally {
		await browser.close();
	}
});
