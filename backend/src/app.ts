import express from 'express';
import cors from 'cors';
import http, { type IncomingHttpHeaders } from 'node:http';
import https from 'node:https';

const HOP_BY_HOP = new Set([
	'connection',
	'keep-alive',
	'proxy-authenticate',
	'proxy-authorization',
	'te',
	'trailer',
	'transfer-encoding',
	'upgrade',
]);

function forwardingHeaders(headers: IncomingHttpHeaders): IncomingHttpHeaders {
	const excluded = new Set(HOP_BY_HOP);
	for (const name of (headers.connection ?? '').split(','))
		excluded.add(name.trim().toLowerCase());
	return Object.fromEntries(Object.entries(headers).filter(([name]) => !excluded.has(name)));
}

/** Keep uploads, SSE, and downloaded files as streams; JSON parsing belongs to the agent service. */
export function createApp(agentUrl = process.env.AGENT_SERVICE_URL || 'http://127.0.0.1:8001') {
	const upstreamBase = new URL(agentUrl);
	if (
		!['http:', 'https:'].includes(upstreamBase.protocol) ||
		upstreamBase.username ||
		upstreamBase.password ||
		upstreamBase.pathname !== '/' ||
		upstreamBase.search ||
		upstreamBase.hash
	) {
		throw new Error(
			'AGENT_SERVICE_URL must be an HTTP(S) origin, such as http://127.0.0.1:8001',
		);
	}
	const app = express();
	app.disable('x-powered-by');
	app.use(cors());
	app.get('/api/health', (_req, res) => res.json({ status: 'ok' }));

	app.use('/api/studio', (req, res) => {
		const target = new URL(upstreamBase.origin);
		const separator = req.originalUrl.indexOf('?');
		target.pathname = separator < 0 ? req.originalUrl : req.originalUrl.slice(0, separator);
		target.search = separator < 0 ? '' : req.originalUrl.slice(separator);
		const headers = forwardingHeaders(req.headers);
		headers.host = target.host;
		delete headers.forwarded;
		delete headers['x-forwarded-for'];
		delete headers['x-forwarded-host'];
		delete headers['x-forwarded-proto'];
		const request = target.protocol === 'https:' ? https.request : http.request;
		const upstream = request(target, { method: req.method, headers }, (response) => {
			res.status(response.statusCode ?? 502);
			for (const [name, value] of Object.entries(forwardingHeaders(response.headers))) {
				if (value !== undefined) res.setHeader(name, value);
			}
			if (String(response.headers['content-type']).includes('text/event-stream')) {
				res.setHeader('Cache-Control', 'no-cache, no-transform');
				res.setHeader('X-Accel-Buffering', 'no');
				res.flushHeaders();
			}
			response.on('error', () => res.destroy());
			response.pipe(res);
		});
		upstream.setTimeout(120_000, () => upstream.destroy(new Error('Agent service timeout')));
		upstream.on('error', () => {
			if (res.destroyed) return;
			if (res.headersSent) {
				res.destroy();
				return;
			}
			res.status(502).json({
				detail: 'PPT 智能体服务暂时不可用，请确认已启动 pnpm dev:agent 后重试。',
			});
		});
		req.on('aborted', () => upstream.destroy());
		res.on('close', () => {
			if (!res.writableEnded) upstream.destroy();
		});
		req.pipe(upstream);
	});
	app.use((_req, res) => res.status(404).json({ detail: '接口不存在' }));
	return app;
}
