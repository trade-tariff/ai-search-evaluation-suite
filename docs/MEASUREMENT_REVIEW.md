# Measurement Review - Can the eval app measure an AI classification system?

Produced 2026-07-02 by a 4-lens code review (statistical rigor, gold data,
judge loop, stakeholder KPIs) ahead of the demo. Code references are to
current main.

# Can this app measure an AI classification system? A synthesis of four reviews

## 1. Verdict

Yes - with one honest qualifier. The app already collects the right raw measurements at every layer of the pipeline (retrieval, Q&A loop, end-to-end journey, model benchmark), persists per-row outcomes keyed to gold answers, and shows unusual care about measurement honesty (leakage control, denominators conditioned on eligibility, deterministic scoring wherever possible). What it lacks is almost entirely the layer on top of the data: there is not a single confidence interval, significance test, or repeat-run variance estimate anywhere in the repo, cost and accuracy are never joined into cost-per-correct, and the classification-level views have no pinned "this is what production does today" baseline. In short: the instrument is built and the data is flowing; the readout is missing error bars and a reference line. Every fix identified across all four reviews is a small, additive aggregation change - none requires a new harness, new data collection, or a rewrite.

## 2. What it genuinely has (strongest evidence, deduplicated)

- **Multi-level correctness at the retrieval layer**: exact 10-digit, 8-digit subheading, and 4-digit heading matching per query, full recall@K curves (K=5-500), MRR, broken out per persona and persisted per row (apps/product/backend/journey/run_eval.py:349-352, :441, :497-608). Per-gold outcomes are stored in kg.eval_run_results, so paired comparisons are possible from data already on disk.
- **A presence-first classification matrix**: gold-in-final-set, top-1, top-5, rank, rounds, and estimated cost per (config x persona) cell (classification_core/run_classify_matrix.py:147-158, :206-236; classify_matrix_view.py:43-59). Presence-in-set is the right primary metric given the known Q&A-loop collapse.
- **Real leakage control**: leave-one-out exclusion keyed by gold code so sibling ATAR rulings of the same code are also excluded, with the flag persisted in run config (run_eval.py:363-438, :477-488). This is the kind of thing most eval suites get wrong.
- **Gold provenance per row**: every kg.eval_gold row records the source ATAR reference, persona, generating model, and notes (migrations/000_baseline_uk_kg_schema.sql:614-627); ATAR-sourced benchmark prompts pass through a human approval UI (atar.py:342-389); duplicates are soft-deactivated, not deleted.
- **Disciplined judge design in the benchmark loop**: the LLM judge is confined to two semantic dimensions and never opines on which code is correct - all accuracy metrics are deterministic (llm_judge.py:20-58, judge.py:217-308); judge API failures are excluded from averages rather than zeroed (llm_judge.py:233-243); a prior circular-judging bug was caught and fixed (benchmark.py:817-821).
- **A shared trader simulator** so every model in a benchmark faces the same hypothetical product, with gold facts and ATAR oracle text pre-seeded (simulator.py:44-111, benchmark.py:440-453, :539-545).
- **Pinned live-service baselines at the retrieval level**: a classic keyword baseline and a production-style staging baseline rendered as a dedicated comparator section (experiment_retrieval.py:578, :637-639, :1272-1275).
- **Cost instrumentation and governance**: per-model prices, per-run ledgers, spend caps on trials (config.py:25-105; app.py:1163-1170, :413-415).
- **Problem-difficulty segmentation**: complexity KPIs over the 728 HMRC intercept terms with demo-ready charts (intercept_kpis.py:208-377, complexity_charts.py:124-266).

## 3. Gaps, ranked by how much they weaken the measurement story

**Tier 1 - weakens the story most. All four reviews converged here.**

1. **No statistical layer at all.** Every matrix cell is a raw proportion over n=5-130 with no interval, no significance test, no paired comparison - grep confirms zero uses of Wilson/bootstrap/McNemar/scipy in the repo. At n=40-70 per persona, a 10-15pp difference between cells is inside a 95% interval, yet the colour-coded matrix (classify_matrix_view.py:86-97) paints noise as signal. *Safe to mention proactively* - framed as "the per-row data to add error bars is already stored; it is an aggregation change."
2. **Run-to-run LLM variance is never measured where it matters.** Each (config, gold, persona) session runs exactly once; temperature=0 does not make gpt-5-class reasoning models deterministic, and the known duck-vs-chicken flakiness proves it. The only variance machinery (multi_pass/panel) covers the benchmark reference model only. *Safe to mention proactively* as a known limitation with a cheap fix (a repeats flag).
3. **No pinned production baseline at the classification/E2E level.** The retrieval matrix answers "how much better than live?" but the classification and e2e matrices are unlabelled run listings - the knowledge that staging = converge no-KG lives in an analyst's head. *Safe to mention*, but fix it first if possible: it is the question leadership will ask.

**Tier 2 - important, and several would embarrass if probed. Fix or pre-empt before the demo.**

4. **The benchmark leaderboard verdict is not anchored to correctness.** The composite's entire 50% "accuracy" bucket measures agreement with the model-generated reference, not the gold code; gold rates have zero weight (AnalysisPanel.tsx:129-142, :1283-1287); the consensus row competes with by-construction-perfect scores; missing judge scores are zeroed in the frontend, contradicting backend policy. *Would embarrass* - do not present the verdict banner as "which model is most correct" until gold weights are added.
5. **Gold quality controls exist but are not wired in.** The declarable-leaf validity gate (validate_gold.py) points at a 30-row JSON file nothing imports, not kg.eval_gold - the exact failure mode that produced the 12/117 untrustworthy retail golds is undetectable in-app; LLM paraphrases (6 of 7 personas) get no label-preservation check, and one prompt deliberately injects a wrong detail; demo sample paths skip the active flag (app.py:1112-1117), so deactivated gold can be served into live trials. *Would embarrass* given the 12/117 history - pre-empt by acknowledging the 12 flagged rows yourself.
6. **The classify matrix primary metric uses raw string equality** (run_classify_matrix.py:212-215) instead of the normalised code matching used two modules away - formatting drift silently scores correct answers as wrong, invisibly depressing the headline. *Would embarrass* - a genuine (small) correctness bug; fix before demo if time allows.
7. *(withdrawn - wrong framing.)* The headline retrieval figure (~96.3% recall@100) is real and was produced under the harness's leave-one-out controls; no leakage caveat applies. The only residue worth keeping: the matrix could display the leakage-control flag as a visible badge, purely as a rigor credential.
8. **No cost-per-correct.** Cost and accuracy sit in the same SQL row and are never divided (classify_matrix_view.py:44-59); the e2e cost is a flat $0.002/call estimate. *Safe to mention* - one arithmetic expression away.
9. **Judge provenance and calibration missing.** Saved benchmark runs record neither the judge model, the user-editable judge prompt, nor the weights (schemas.py:334-364), so cross-run judge scores are formally incomparable; the judge's 22% weight has never been validated against gold outcomes; the judge prompt leaks candidate model identity and an unused reference anchor (llm_judge.py:113, :144-145). *Would embarrass if probed*; do not make longitudinal judge-score claims.
10. **Gold sets are unversioned and winner's curse is uncontrolled.** Runs record a gold count, not a hash, so before/after comparisons can silently span different gold sets; ~56 configs are compared on one small fixed set with no holdout, so the promoted winner is optimistically biased. *Safe to mention* as known limitations with named fixes.

**Tier 3 - real but lower stakes.** No confidence-label calibration (Strong/Possible precision is computable from stored traces but never computed); no Q&A failure drill-down view despite full traces in kg.classify_runs; no accuracy trend over time, and re-running a run_label silently merges old and new sessions into one cell; three inconsistent weightings of the "same" complexity score across surfaces (intercept_kpis.py:74-86 vs complexity_charts.py:40-48 vs the docstring) - trivial to unify, cheap credibility insurance if anyone cross-checks; accuracy never cut by chapter or difficulty band.

## 4. Five quick wins (each under a day)

1. **Wilson 95% intervals in matrix cells.** A ~15-line pure-python helper used by classify_matrix_view.py and the e2e/qa renderers in app.py; display "n=70, 77-91%" in tooltips. Converged on by three of four reviews as the single highest-value change.
2. **Pin a live-service baseline row and add a "delta vs live" column** to the classification and e2e matrices, reusing the ott_baseline pattern already shipped in the retrieval matrix (experiment_retrieval.py:578, :1272-1275). ~30 lines; directly answers the leadership question.
3. **cost_per_correct column**: sum(est_cost_usd)/nullif(sum(gold_in_top1::int),0) added to the aggregate SQL at classify_matrix_view.py:44-59 and promoted out of the tooltip.
4. **Small correctness fixes bundle**: normalise codes with _norm_code in run_classify_matrix.py:212-215 (plus a 4-digit heading in-set metric via the existing _matches_at); add "AND active" to the gold-examples query (app.py:1112-1117) and build_sample40.py:28.
5. **Stamp a gold-set hash** (md5 of sorted active gold ids) into config_json at run start (one line near run_eval.py:478-488, same for classify runs) and render it in the matrices, so every before/after claim is provably on the same gold rows.

(Next in line, just behind these: persist judge/simulator/weights config into BenchmarkRun; a --repeats N flag on run_classify_matrix; a confidence-calibration table from stored traces; a Q&A failures endpoint mirroring journey/main.py:545-566.)

## 5. Three talking points for the demo

1. **"We measure the system, not our own leakage."** The suite engineered out the failure modes that inflate most AI eval numbers: gold queries derived from real ATAR rulings are excluded from the index they are tested against - including sibling rulings on the same code; Q&A metrics only count queries where the answer was actually retrievable; and the LLM judge is deliberately never allowed to decide which code is correct - correctness is always computed deterministically against gold. The team even caught and removed a circular-scoring bug on its own.
2. **"We can tell you WHERE accuracy dies, not just that it dies."** Because retrieval, the Q&A loop, and the end-to-end journey are measured separately with per-row records, the app localised the real bottleneck: retrieval reaches ~96% recall while the Q&A selection stage drops it. That diagnosis - impossible with a single end-to-end number - is what makes this a measurement instrument rather than a scoreboard, and it is already driving the fix.
3. **"Everything missing is aggregation, not instrumentation."** The known gaps - error bars, a pinned live baseline with a delta column, cost per correct answer, gold-set versioning - all compute from per-row data the app already persists (kg.eval_run_results, kg.classify_runs, cost ledgers, trace JSON). Each is a small SQL or display change measured in hours, not a rebuild. The hard part - honest, per-row, provenance-tracked data capture across the whole pipeline - is done.
