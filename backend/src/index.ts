import { createApp } from './app';

const port = Number(process.env.PORT || 3002);
const host = process.env.HOST || '0.0.0.0';
const app = createApp();

const server = app.listen(port, host, () => {
	console.log(`PPT Studio BFF listening on http://${host}:${port}`);
});

for (const signal of ['SIGTERM', 'SIGINT']) {
	process.on(signal, () => {
		server.close(() => process.exit(0));
		setTimeout(() => process.exit(0), 5000).unref();
	});
}
