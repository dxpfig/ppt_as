const { spawnSync } = require('node:child_process');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const frontend = path.join(root, 'frontend');
const files = process.argv
	.slice(2)
	.map((file) => path.resolve(root, file))
	.filter((file) => file.startsWith(frontend + path.sep))
	.map((file) => path.relative(frontend, file));

if (!files.length) process.exit(0);

const result = spawnSync(
	process.execPath,
	[path.join(frontend, 'node_modules', 'eslint', 'bin', 'eslint.js'), '--fix', ...files],
	{ cwd: frontend, stdio: 'inherit' },
);
process.exit(result.status ?? 1);
