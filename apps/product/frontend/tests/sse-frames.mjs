import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
const source = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } });
const { startBenchmarkSSE } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const text = ': heartbeat\r\n\r\nevent: benchmark:start\r\ndata: {"label":"café"}\r\n\r\nevent: broken\ndata: invalid\n\ndata: {"default":true}\n\nevent: model:complete\ndata: {\ndata: "ok":true}\n\nevent: benchmark:complete\ndata: {}\n\nevent: incomplete\ndata: {}';
const bytes = new TextEncoder().encode(text);
for (const size of [1, 2, 7, bytes.length]) {
  const events = [];
  globalThis.fetch = async () => new Response(new ReadableStream({ start(controller) {
    for (let i = 0; i < bytes.length; i += size) controller.enqueue(bytes.slice(i, i + size));
    controller.close();
  }}));
  await new Promise((resolve, reject) => startBenchmarkSSE([], [], (event, data) => events.push([event, data]), resolve, reject));
  assert.deepEqual(events, [['benchmark:start', {label: 'café'}], ['message', {default: true}], ['model:complete', {ok: true}], ['benchmark:complete', {}]]);
}
console.log('SSE frame regressions passed (1-byte, split, multi-frame, CRLF, UTF-8, malformed and incomplete frames).');
