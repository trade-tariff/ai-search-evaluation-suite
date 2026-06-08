# Suite Primer Run

This run primes the deployed app with saved, reviewable benchmark evidence instead of leaving Simulator, Judge, Benchmark, Analysis, and Financial panels empty or abstract.

The run is intentionally split into two stages:

1. `prep`: broad, cheap screening with a small model. This finds bad prompts, malformed fact sheets, weak examples, broken app paths, and retrieval failures.
2. `prod_check`: smaller confirmation pass with the configured production model as the reference. This is the only stage used for production-behaviour claims.

The runner is dry-run by default. Paid execution requires `--execute` and `--max-usd`.

## What We Measure

| Area | Metric | Why it matters | Expected signal |
|---|---:|---|---|
| Corpus coverage | prompts with `gold_code`, oracle text, and seeded facts | Confirms the benchmark set is grounded enough for simulator and judge use | Seeded prompts should dominate the selected set |
| Retrieval quality | top-1, top-3, MRR, heading match, top-5 overlap, hierarchical score | Shows whether the retrieval stack puts the right goods code close enough for downstream reasoning | Production reference should beat cheap screening on hard prompts |
| Question behaviour | rounds, question count, question efficiency, seeded fact-store hit rate | Shows whether models ask useful questions instead of burning turns | Higher hit rate and fewer repeated slots are better |
| Simulator quality | simulator failures, store hits, simulator cost | Confirms the simulator is answering from seeded facts/oracle text and not drifting | Failures should be near zero on seeded prompts |
| Judge quality | fact consistency and question quality | Gives semantic quality checks that deterministic metrics cannot see | Production-reference runs should have stable judge scores |
| Cost and latency | total cost, cost per prompt, latency per prompt, provider errors | Keeps the EC2 run useful without quiet overspend | Spend stays below cap; errors are explicit |
| Failure clusters | missing gold, wrong heading, bad schema, excessive questions, high-cost cases | Turns a run into actionable app data rather than a vanity score | Produces a short list of cases to fix or feature in demos |

## Why This Helps

The app already has the mechanics: Search References, KG/facets, simulator, judge, benchmark exports, cost panels, and saved-run drilldowns. What it needs for a demo or review session is populated evidence:

- seeded examples that show why the simulator exists;
- saved judge results that make the judge panel concrete;
- benchmark runs that make Analysis and Financial panels meaningful;
- failure cases that show where retrieval, prompts, or KG enrichment should improve;
- a production-model comparison that separates cheap screening from real behaviour.

## Run Shape

Default dry-run plan:

- `prep`: up to 24 seeded prompts, reference `gpt-5-nano`, candidate `gpt-5-mini`.
- `prod_check`: up to 6 seeded prompts, reference from `--prod-model`, candidate `gpt-5-mini`.
- OpenSearch prompt context limit: 80.
- Judge and simulator: `gpt-5-nano`, low effort, temperature 0.
- Output folder: `apps/product/data/primer_runs/`.

The production check defaults to `gpt-5.5`. Pass it explicitly when you want the manifest to make the production reference obvious, or override it for another reference model:

```bash
python apps/product/backend/suite_primer.py --url http://127.0.0.1:8000 --prod-model gpt-5.5 --dry-run
```

To execute with a spend guard:

```bash
python apps/product/backend/suite_primer.py --url http://127.0.0.1:8000 --prod-model gpt-5.5 --max-usd 3 --execute
```

For a protected endpoint, set either:

```bash
export AI_SEARCH_BASIC_AUTH="user:password"
```

or:

```bash
export BASIC_AUTH_USER="user"
export BASIC_AUTH_PASSWORD="password"
```

## Spend Controls

- Dry-run is default.
- `--execute` refuses to run without a positive `--max-usd`.
- The runner writes a manifest before provider-backed calls.
- The runner sends `allow_spend=true` only for benchmark starts.
- The runner watches streamed completion costs and cancels if observed spend crosses the cap.
- Simulator and judge totals are only complete when the benchmark result is aggregated, so the runner records final reported cost and will not start the next stage if the cap is consumed.
- Secrets are read from environment only and are never written to logs.
- Saved run data is app runtime data, not committed source.

## Expected Outcome

After one bounded run, the app should have:

- at least one completed broad screening run;
- one small production-reference run, if the production model id is configured;
- benchmark JSON/CSV export available;
- Analysis and Financial panels populated with real cost and quality data;
- Simulator and Judge panels backed by saved examples rather than abstract controls;
- a concise manifest explaining exactly what was measured and what remains weak.
