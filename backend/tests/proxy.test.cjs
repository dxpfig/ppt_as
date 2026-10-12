const assert = require('node:assert/strict');
const http = require('node:http');
const { test } = require('node:test');
const { once } = require('node:events');
const { createApp } = require('../dist/app.js');

async function listen(server) {
	server.listen(0, '127.0.0.1');
	await once(server, 'listening');
	return `http://127.0.0.1:${server.address().port}`;
}

function cleanup(t, server) {
	t.after(() => {
		server.closeAllConnections();
		server.close();
	});
}

async function setup(t, handler) {
	const upstream = http.createServer(handler);
	cleanup(t, upstream);
	const upstreamUrl = await listen(upstream);
	const bff = http.createServer(createApp(upstreamUrl));
	cleanup(t, bff);
	return { base: await listen(bff), upstream };
}

test('preserves multipart bytes, path/query, headers, status and binary download', async (t) => {
	const bytes = Buffer.from([0x50, 0x4b, 0, 255, 10, 13, 128, 42]);
	const body = Buffer.concat([
		Buffer.from(
			'--test-boundary\r\nContent-Disposition: form-data; name="file"; filename="company.pptx"\r\nContent-Type: application/octet-stream\r\n\r\n',
		),
		bytes,
		Buffer.from('\r\n--test-boundary--\r\n'),
	]);
	const { base } = await setup(t, async (req, res) => {
		assert.equal(req.url, '/api/studio/templates?style=company%20brand');
		assert.equal(req.headers['content-type'], 'multipart/form-data; boundary=test-boundary');
		assert.equal(req.headers['x-forwarded-for'], undefined);
		const chunks = [];
		for await (const chunk of req) chunks.push(chunk);
		assert.deepEqual(Buffer.concat(chunks), body);
		res.writeHead(201, {
			'content-type':
				'application/vnd.openxmlformats-officedocument.presentationml.presentation',
			'content-disposition': 'attachment; filename="company.pptx"',
		});
		res.end(bytes);
	});
	const response = await fetch(`${base}/api/studio/templates?style=company%20brand`, {
		method: 'POST',
		headers: {
			'content-type': 'multipart/form-data; boundary=test-boundary',
			'x-forwarded-for': 'untrusted',
		},
		body,
	});
	assert.equal(response.status, 201);
	assert.equal(
		response.headers.get('content-disposition'),
		'attachment; filename="company.pptx"',
	);
	assert.deepEqual(Buffer.from(await response.arrayBuffer()), bytes);
});

test('streams events immediately, forwards reconnect cursor and cancels upstream on disconnect', async (t) => {
	let closeUpstream;
	const closed = new Promise((resolve) => {
		closeUpstream = resolve;
	});
	const { base } = await setup(t, (req, res) => {
		assert.equal(req.headers['last-event-id'], '7');
		res.writeHead(200, { 'content-type': 'text/event-stream' });
		res.write('id: 8\nevent: progress\ndata: {"stage":1}\n\n');
		res.on('close', closeUpstream);
		// Intentionally never end: a buffering proxy would hang the client read.
	});
	const abort = new AbortController();
	const response = await fetch(`${base}/api/studio/jobs/test/events`, {
		headers: { 'Last-Event-ID': '7' },
		signal: AbortSignal.any([abort.signal, AbortSignal.timeout(3000)]),
	});
	assert.equal(response.headers.get('x-accel-buffering'), 'no');
	const reader = response.body.getReader();
	assert.match(new TextDecoder().decode((await reader.read()).value), /id: 8/);
	abort.abort();
	await Promise.race([
		closed,
		new Promise((_, reject) => {
			const timer = setTimeout(
				() => reject(new Error('Upstream connection remained open')),
				2000,
			);
			timer.unref();
		}),
	]);
});

test('preserves upstream validation errors without requiring JSON parsing in the BFF', async (t) => {
	const { base } = await setup(t, (req, res) => {
		res.writeHead(422, { 'content-type': 'application/json' });
		res.end(JSON.stringify({ detail: '模板文件无效' }));
	});
	const response = await fetch(`${base}/api/studio/templates`, {
		method: 'POST',
		body: 'invalid',
	});
	assert.equal(response.status, 422);
	assert.deepEqual(await response.json(), { detail: '模板文件无效' });
});

test('offline agent returns actionable 502; BFF health still works', async (t) => {
	const { base, upstream } = await setup(t, (_req, res) => res.end());
	await new Promise((resolve) => upstream.close(resolve));
	const response = await fetch(`${base}/api/studio/status`);
	assert.equal(response.status, 502);
	assert.match((await response.json()).detail, /pnpm dev:agent/);
	assert.deepEqual(await (await fetch(`${base}/api/health`)).json(), { status: 'ok' });
	assert.equal((await fetch(`${base}/api/missing`)).status, 404);
});

test('rejects invalid upstream configuration', () => {
	for (const url of [
		'file:///tmp/agent',
		'http://user:password@localhost',
		'http://localhost/subpath',
	]) {
		assert.throws(() => createApp(url), /HTTP\(S\) origin/);
	}
});
