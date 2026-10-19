import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
let source = await readFile(new URL('../src/components/BenchmarkPanel.tsx', import.meta.url), 'utf8');
source = source.replace('  const formatLog =', '  globalThis.inspect = { start, stop, running, done, outcomeMessage }; return null;\n  const formatLog =');
const { outputText } = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } });
let state = [], cursor = 0, callbacks;
const react = {
  useState(initial) { const i = cursor++; if (!(i in state)) state[i] = initial; return [state[i], value => state[i] = typeof value === 'function' ? value(state[i]) : value]; },
  useRef(initial) { const i = cursor++; return state[i] ??= {current: initial}; },
};
const api = { startBenchmarkSSE(...args) { callbacks = {event: args[2], eof: args[3], error: args[4]}; return new AbortController(); }, cancelBenchmark: async () => {} };
const module = {exports: {}};
new Function('require', 'module', 'exports', outputText)(name => name === 'react' ? react : name === '../api' ? api : {}, module, module.exports);
globalThis.confirm = () => true;
function render() { cursor = 0; module.exports.default({promptIndices: [0], modelIds: ['fixture'], opensearchLimit: 1}); return globalThis.inspect; }
function start() { state = []; render().start(); return callbacks; }
let cb = start(); cb.eof(); assert.equal(render().done, false); assert.match(render().outcomeMessage, /ended before completion/);
cb = start(); cb.event('error', {message: 'offline failure'}); cb.eof(); assert.equal(render().done, false); assert.match(render().outcomeMessage, /offline failure/);
cb = start(); cb.event('benchmark:cancelled', {}); cb.eof(); assert.equal(render().done, false); assert.match(render().outcomeMessage, /cancelled/);
cb = start(); cb.error('HTTP 503'); assert.equal(render().done, false); assert.match(render().outcomeMessage, /HTTP 503/);
cb = start(); await render().stop(); cb.eof(); cb.event('benchmark:complete', {}); assert.equal(render().done, false); assert.match(render().outcomeMessage, /cancelled/);
cb = start(); cb.event('benchmark:complete', {}); cb.eof(); assert.equal(render().done, true); assert.equal(render().outcomeMessage, null);
cb = start(); const old = cb; render().start(); old.event('benchmark:complete', {}); old.eof(); assert.equal(render().done, false); callbacks.event('benchmark:complete', {}); assert.equal(render().done, true);
console.log('Benchmark outcome regressions passed (success, SSE/HTTP errors, cancel, premature EOF, superseded stream).');
