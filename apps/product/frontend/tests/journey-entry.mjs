import assert from 'node:assert/strict';
import {readFile, readdir} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
process.chdir(fileURLToPath(new URL('..', import.meta.url)));
for (const journey of [false, true]) {
  execFileSync('npm', ['run', 'build', ...(journey ? ['--', '--mode', 'journey'] : [])], {stdio: 'inherit'});
  const html = await readFile('dist/index.html', 'utf8');
  const scripts = (await readdir('dist/assets')).filter(name => name.endsWith('.js'));
  const bundle = (await Promise.all(scripts.map(name => readFile(`dist/assets/${name}`, 'utf8')))).join('\n');
  assert.match(html, journey ? /<title>Trader Journey<\/title>/ : /<title>AI Search Evaluation Suite<\/title>/);
  assert.equal(bundle.includes('1. What are you importing?'), journey);
  assert.equal(bundle.includes('Retrieval experiment workbench'), !journey);
}
const dockerfile = await readFile('../Dockerfile.journey', 'utf8');
assert.match(dockerfile, /RUN npm run build -- --mode journey/);
console.log('Journey entry regression passed (default workbench preserved; journey bundle renders classification and excludes workbench).');
