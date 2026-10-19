import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import ts from 'typescript';
function deferred() { let resolve, reject; const promise = new Promise((a, b) => {resolve = a; reject = b;}); return {promise, resolve, reject}; }
async function component(file, marker, expose, api = {}) {
  let source = await readFile(new URL(`../src/components/${file}.tsx`, import.meta.url), 'utf8');
  source = source.replace(marker, `globalThis.inspect = {${expose}}; return null;\n${marker}`);
  const {outputText} = ts.transpileModule(source, {compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS}});
  const slots = []; let cursor = 0, effects = [];
  const react = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = initial; return [slots[i], value => slots[i] = typeof value === 'function' ? value(slots[i]) : value]; },
    useRef(initial) { const i = cursor++; return slots[i] ??= {current: initial}; },
    useMemo(fn) { return fn(); },
    useEffect(fn, deps) { const i = cursor++; const prior = slots[i]; slots[i] = deps; if (deps.length && (!prior || deps.some((v, j) => v !== prior[j]))) effects.push(fn); },
  };
  const module = {exports: {}};
  new Function('require', 'module', 'exports', outputText)(name => name === 'react' ? react : api, module, module.exports);
  return () => { cursor = 0; module.exports.default(); const pending = effects; effects = []; pending.forEach(fn => fn()); return globalThis.inspect; };
}
const A = {ref: 'A', status: 'pending', gold_facts: [{slot: 'material', answer: 'A facts'}]};
const B = {ref: 'B', status: 'pending', gold_facts: [{slot: 'material', answer: 'B facts'}]};
let regen = deferred(), approved;
const atar = await component('AtarPanel', '  const counts =', 'setDrafts, selectDraft, regenFacts, approve, factEdits, actionMsg, regenBusy', {
  regenerateAtarFacts: () => regen.promise,
  approveAtarDraft: async (ref, facts) => {approved = {ref, facts}; return {prompt_index: 1};},
  listAtarDrafts: async () => ({drafts: [A, B]}),
});
atar().setDrafts([A, B]); atar().selectDraft('A'); atar();
const oldRegen = atar().regenFacts(); atar().selectDraft('B'); atar();
regen.resolve({gold_facts: [{slot: 'material', answer: 'late A facts'}]}); await oldRegen;
assert.deepEqual(atar().factEdits, B.gold_facts); assert.equal(atar().actionMsg, '');
await atar().approve(); assert.deepEqual(approved, {ref: 'B', facts: B.gold_facts});
regen = deferred(); atar().selectDraft('A'); atar(); const oldError = atar().regenFacts(); atar().selectDraft('B'); atar(); regen.reject(new Error('late A failure')); await oldError; assert.equal(atar().actionMsg, '');
regen = deferred(); const valid = atar().regenFacts(); regen.resolve({gold_facts: [{slot: 'material', answer: 'new B facts'}]}); await valid; assert.equal(atar().factEdits[0].answer, 'new B facts');
const lab = await component('ExperimentsTab', '  const rankTone =', 'runTrial, hydrateTrial, invalidateTrial, resetHydratedQa, setExperiments, setSelectedRunLabel, setQuery, setTrial, trial, hydration, trialError, hydrationError, running, hydrating');
let h = lab(); h.setExperiments([{run_label: 'fixture', runnable: true, config: {}}]); h.setSelectedRunLabel('fixture'); h.setQuery('A');
let request = deferred(); globalThis.fetch = () => request.promise;
const oldTrial = lab().runTrial({preventDefault() {}}); lab().invalidateTrial(); lab().setQuery('B');
request.resolve(new Response(JSON.stringify({query: 'A'}))); await oldTrial; assert.equal(lab().trial, null);
request = deferred(); const trialB = lab().runTrial({preventDefault() {}}); request.resolve(new Response(JSON.stringify({query: 'B', candidates: [{commodity_code: '1234567890'}]}))); await trialB; assert.equal(lab().trial.query, 'B');
request = deferred(); const oldHydration = lab().hydrateTrial(); lab().invalidateTrial(); request.resolve(new Response(JSON.stringify({query: 'B'}))); await oldHydration; assert.equal(lab().hydration, null);
lab().setTrial({query: 'C', candidates: [{commodity_code: '1234567890'}]}); request = deferred(); const staleError = lab().hydrateTrial(); lab().resetHydratedQa(); request.reject(new Error('late hydration error')); await staleError; assert.equal(lab().hydrationError, null); assert.equal(lab().hydrating, false);
request = deferred(); const current = lab().hydrateTrial(); request.resolve(new Response(JSON.stringify({query: 'C', hydrated: []}))); await current; assert.equal(lab().hydration.query, 'C');
console.log('Stale response regressions passed (ATaR A/B approval, delayed success/error, current success, retrieval and hydration invalidation).');
