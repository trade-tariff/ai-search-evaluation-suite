import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
const source = await readFile(new URL('../src/components/AnalysisPanel.tsx', import.meta.url), 'utf8');
const start = source.indexOf('{comparing && compareResults && (() => {') + '{comparing && compareResults && (() => {'.length;
const body = source.slice(start, source.indexOf('})()}', start));
const { outputText } = ts.transpileModule(`export function report(summaries: any[], compareResults: any) { ${body} }`, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } });
const module = { exports: {} };
const jsx = (type, props) => ({type, props});
new Function('require', 'module', 'exports', outputText)(() => ({jsx, jsxs: jsx}), module, module.exports);
function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (Array.isArray(node)) return node.map(text).join(' ');
  if (typeof node === 'object') return text(node.props.children);
  return String(node);
}
const model = (id, value) => ({model_id: id, model_name: id, top1_accuracy: value});
const report = (after, before) => text(module.exports.report(after, {summaries: before}));
assert.match(report([model('A', null)], [model('A', null)]), /No comparable reference top-1/);
assert.doesNotMatch(report([model('A', null)], [model('A', null)]), /within/);
assert.match(report([model('A', .8)], [model('A', .81)]), /1 compared model/);
assert.match(report([model('A', null), model('B', .8)], [model('A', null), model('B', .81)]), /Not compared.*A/);
assert.match(report([model('A', .6)], [model('A', .8)]), /1.*regression/);
assert.match(report([model('A', .8)], [model('A', .6)]), /1.*improvement/);
assert.match(report([model('A', null)], [model('A', .8)]), /No comparable/);
assert.match(report([model('new', null)], [model('old', null)]), /Removed:.*old.*Added:.*new/);
console.log('Gold comparison regressions passed (null, mixed, comparable, regression, improvement, added/removed).');
