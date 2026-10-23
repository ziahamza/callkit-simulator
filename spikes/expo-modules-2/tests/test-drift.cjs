const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const checker = path.join(root, 'scripts/check-exports.cjs');
const original = fs.readFileSync(path.join(root, 'modules/expo2-spike/index.ts'), 'utf8');
fs.mkdirSync(path.join(root, 'out'), { recursive: true });
const temporary = fs.mkdtempSync(path.join(root, 'out/drift-'));
function run(source) {
  const filename = path.join(temporary, 'declarations.ts');
  fs.writeFileSync(filename, source);
  return spawnSync(process.execPath, [checker, filename, ...process.argv.slice(2)], { cwd: root, encoding: 'utf8' });
}
try {
  const baseline = run(original);
  assert.equal(baseline.status, 0, baseline.stderr);
  const mutations = [
    ['removed method', original.replace(/  publish\(version: number\): void;\n/, '')],
    ['parameter type', original.replace('publish(version: number)', 'publish(version: string)')],
    ['record field', original.replace('connectedAt: number', 'connectedAt: string')],
    ['module base', original.replace('extends NativeModule<Events>', 'extends DriftBase<Events>') +
      "\nimport { SharedObject as DriftBase } from 'expo-modules-core';\n"],
  ];
  if (original.includes('class PingProbe extends SharedObject'))
    mutations.push(['shared base', original.replace('extends SharedObject<', 'extends NativeModule<')]);
  for (const [name, source] of mutations) {
    assert.notEqual(source, original, `mutation did not apply: ${name}`);
    const result = run(source);
    assert.equal(result.status, 1, `${name}: ${result.stderr}`);
    assert.match(result.stderr, /export drift/, `${name}: checker must report drift`);
  }
  console.log('Export drift: baseline and declaration negative controls passed');
} finally {
  fs.rmSync(temporary, { recursive: true, force: true });
}
