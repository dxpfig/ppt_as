/** Assess an available YAML 1.2 parser without adopting it as a production dependency. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const library = process.env.PPTD_YAML_MODULE || path.resolve(__dirname, '../../node_modules/.pnpm/yaml@2.9.0/node_modules/yaml');
const YAML = require(library);
const sample = '# preserved comment\ncontent:\n  text: on # retained inline comment\nnumber: 012\nscientific: 1e3\ndate: 2026-10-09\n';
const document = YAML.parseDocument(sample, { version: '1.2', uniqueKeys: true });
const parsed = document.toJS();
assert.deepEqual(parsed, { content: { text: 'on' }, number: 12, scientific: 1000, date: '2026-10-09' });
document.setIn(['content', 'text'], '编辑后');
const saved = String(document);
assert.ok(saved.includes('# retained inline comment'));
const duplicateKeysRejected = YAML.parseDocument('a: one\na: two\n', { uniqueKeys: true }).errors.length > 0;
assert.ok(duplicateKeysRejected);
const fixture = path.join(__dirname, 'fixture');
const entry = YAML.parse(fs.readFileSync(path.join(fixture, 'probe.pptd'), 'utf8'));
const fixtures = ['probe.pptd', ...entry.pages].map(file => {
  const value = YAML.parseDocument(fs.readFileSync(path.join(fixture, file), 'utf8'), { version: '1.2', uniqueKeys: true });
  assert.equal(value.errors.length, 0);
  assert.deepEqual(value.toJS(), YAML.parse(String(value)));
  return { file, semanticRoundTrip: true };
});
const report = { package: 'yaml', version: require(path.join(library, 'package.json')).version, scope: 'candidate YAML core, syntax probes only; no PPTD schema or rendering validation', input: sample, parsed, saved, duplicateKeysRejected, fixtures };
const destination = path.resolve(__dirname, '../../runtime/pptd-v2-spike/yaml-candidate-report.json');
fs.mkdirSync(path.dirname(destination), { recursive: true });
fs.writeFileSync(destination, JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify({ status: 'passed', report: destination }));
