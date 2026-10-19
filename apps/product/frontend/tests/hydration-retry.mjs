import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
let source = await readFile(new URL('../src/components/ExperimentsTab.tsx', import.meta.url), 'utf8');
source = source.replace('  const rankTone =', '  globalThis.inspect = { hydrateTrial, answerHydratedQuestion, buildFreshQa, resetHydratedQa, hydration, qaHistory, pendingHydration, hydrationError, setTrial, setHydration, setQaHistory }; return null;\n  const rankTone =');
const {outputText} = ts.transpileModule(source, {compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS}});
const state = []; let cursor = 0;
const react = {
  useState(initial) { const i = cursor++; if (!(i in state)) state[i] = initial; return [state[i], value => state[i] = typeof value === 'function' ? value(state[i]) : value]; },
  useRef(initial) { const i = cursor++; return state[i] ??= {current: initial}; },
  useMemo(fn) { return fn(); }, useEffect() {},
};
const module = {exports: {}};
new Function('require', 'module', 'exports', outputText)(name => name === 'react' ? react : {}, module, module.exports);
function render() { cursor = 0; module.exports.default(); return globalThis.inspect; }
let h = render();
const first = {hydrated: [], question_hint: {question: 'Material?', options_meta: [{label: 'Cotton', value: 'cotton'}], facet_key: 'material'}};
h.setTrial({query: 'fixture goods', retrieval_limit: 10, candidates: [{commodity_code: '1234567890'}]});
h.setHydration(first);
h.setQaHistory([{question: 'Earlier?', answer: 'Yes'}]);
const bodies = []; let fail = true;
globalThis.fetch = async (url, init) => { bodies.push(init.body); return fail ? new Response(JSON.stringify({detail: 'offline failure'}), {status: 503}) : new Response(JSON.stringify({hydrated: [], question_hint: {question: 'Next?'}})); };
await render().answerHydratedQuestion('Cotton');
h = render(); assert.equal(h.hydration, first); assert.equal(h.qaHistory.length, 1); assert.equal(h.pendingHydration.history.length, 2); assert.match(h.hydrationError, /offline failure/);
fail = false; await h.hydrateTrial(h.pendingHydration.history, h.pendingHydration.body);
h = render(); assert.equal(bodies[0], bodies[1]); assert.equal(h.qaHistory.length, 2); assert.equal(h.qaHistory[1].answer, 'Cotton'); assert.equal(h.pendingHydration, null);
const successful = h.hydration;
fail = true; await h.buildFreshQa(); h = render(); assert.equal(h.hydration, successful); assert.equal(h.qaHistory.length, 2); assert.deepEqual(h.pendingHydration.history, []);
fail = false; await h.hydrateTrial(h.pendingHydration.history, h.pendingHydration.body); h = render(); assert.deepEqual(h.qaHistory, []); assert.equal(bodies[2], bodies[3]);
h.resetHydratedQa(); assert.equal(render().pendingHydration, null);
console.log('Hydration retry regressions passed (failed turn/restart retain state; exact retry body; history commits once on success).');
