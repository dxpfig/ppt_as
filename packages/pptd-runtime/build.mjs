import { build } from 'esbuild';
import { createRequire } from 'node:module';
import { cp, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { exportIconIndex } from './src/math-icons.js';

const require = createRequire(import.meta.url);
const root = path.dirname(fileURLToPath(import.meta.url));
const output = path.join(root, 'build');
await mkdir(output, { recursive: true });
await build({
	entryPoints: [path.join(root, 'src/index.js')],
	outfile: path.join(output, 'pptd-runtime.js'),
	bundle: true,
	format: 'iife',
	globalName: 'PPTDRuntime',
	platform: 'browser',
	target: ['chrome100'],
	minify: true,
	sourcemap: true,
	legalComments: 'linked',
});
const katex = path.dirname(require.resolve('katex/dist/katex.min.css'));
await cp(path.join(katex, 'katex.min.css'), path.join(output, 'katex.min.css'));
await cp(path.join(katex, 'fonts'), path.join(output, 'fonts'), { recursive: true });
await cp(
	require.resolve('katex/package.json').replace('package.json', 'LICENSE'),
	path.join(output, 'KATEX-LICENSE'),
);
await writeFile(path.join(output, 'icons.json'), JSON.stringify(exportIconIndex()));
console.log(
	`Built PPTD browser runtime, KaTeX fonts, and ${Object.keys(exportIconIndex()).length} Font Awesome names.`,
);
