# MCP classification_search - eval benchmark (first pass)

> **Correction (after review).** These are *single-call* recall numbers at shallow k (10-20). They
> are a FLOOR, not a measure of how the MCP "fares". The journey matrix is measured at **recall@100**,
> and more importantly, in an MCP+LLM+skill setup the agent makes many calls and curates the shortlist
> itself - so the right metric is end-to-end final-answer **top-1**, not single-call recall. The
> "MCP matches our pipeline" line below holds ONLY at k=20 vs the `desc_only` config; it does NOT mean
> the MCP keeps up with the full 67-config / limit-500 matrix. See `EVAL-DESIGN.md` for the correct
> eval. Treat everything below as a retrieval floor.

**What:** the Trade Tariff MCP `classification_search` scored against `kg.eval_gold`, the
journey project's gold set. Recall@k at four code-precision levels (10-digit exact, 8-digit
subheading, 6-digit, 4-digit heading).

**How:** `mcp-bench/bench_mcp.py` (read-only) - reads the gold dump, calls the live prod MCP per
query (`service=uk`), scores the rank of the expected code in the returned candidates. Sample =
140 queries, 20 per persona across the 7 personas. Ran **locally** against the prod MCP (the
on-VM run is the same script in the journey-app container; it was blocked here only by the
shared-host SSH guardrail). Does not write the database.

## Headline (140-query sample, k=20)

| Level | R@1 | R@5 | R@10 | R@20 |
|---|---|---|---|---|
| exact 10-digit | 0.03 | - | 0.12 | - |
| 8-digit subheading | - | - | 0.14 | 0.21 |
| 6-digit | - | - | 0.31 | - |
| 4-digit heading | - | - | 0.44 | - |

By persona, the right **heading** lands in the top-10 ~35-55% of the time; `naive_branded`
(brand name, no description) is the weakest (heading 0.40, subheading 0.00) - exactly the case
that needs Q&A elicitation. Going from k=20 to the tool's max k=50 barely moves recall (it
plateaus by ~20).

## Like-for-like vs the journey pipeline (same 140 sample, k=20, on the VM)

Ran our own `retrieve_candidates` over the identical sample in the journey-app container (read-only),
two configs:

| Config | R@1 exact | R@10 exact | R@10 sub-8 | R@10 head-4 | R@20 sub-8 | MRR(8) |
|---|---|---|---|---|---|---|
| **MCP classification_search** | 0.03 | 0.12 | 0.14 | 0.44 | 0.21 | 0.09 |
| journey `desc_only` (FTS + desc vector) | 0.01 | 0.10 | 0.19 | 0.39 | 0.26 | 0.11 |
| journey `all_legs` (+ curated + facts + KG) | 0.11 | 0.76 | 0.76 | 0.80 | 0.95 | 0.32 |

1. **On fair, pure-description retrieval the MCP matches our own pipeline.** `desc_only` and the MCP
   are within noise at every level (exact ~0.10-0.12, heading ~0.39-0.44). Neil's hybrid search is
   as good as ours at the retrieval layer - a genuine endorsement of the MCP.
2. **The journey pipeline's big lead is the curated/facts/KG legs, and on this eval they are
   gold-contaminated.** Those legs were seeded from the same ATARs as the gold set, so `all_legs`
   (0.76 exact@10) is largely memorising, not generalising. The honest measure is the leave-one-out
   (LOO) config, which the journey notes put at recall@1 ~0.18. Treat 0.76 as an inflated ceiling.

**Net:** the MCP is already a strong retrieval base, on par with our raw pipeline. The remaining
prize is the KG/structured-knowledge layer - *if* it generalises (LOO-clean), not just memorises.
That is exactly the case for exposing the KG to the MCP, and the LOO eval is how you prove it is real
before shipping it.

## Read

`classification_search` on its own is a **recall front-end**: it reliably narrows to the right
chapter/heading neighbourhood, but rarely surfaces the exact declarable 10-digit code by itself.
That is not a weakness to fix in search - it is the gap the **uk-commodity-code-classifier skill**
is designed to close (section/chapter notes + hierarchy navigation + GIR resolution + Q&A, which
is what the naive/branded personas need). So this result **supports the connector + skills
layering**, rather than undermining the MCP.

## Honest caveats (do not over-read)

- **Raw retrieval only.** No Q&A loop, no notes, no GIR step - just the single search call. The
  skill layer is exactly what's missing here.
- **Hard gold.** `kg.eval_gold` is ATAR-derived: specific, expert, sometimes genuinely debatable
  codes. Example: "mechanical seal" gold = `7326909290` (other steel articles) but the MCP returns
  `8484 20` (mechanical seals) - arguably the better answer, scored as a miss. So recall-vs-gold
  understates correctness.
- **Not yet a like-for-like vs the journey pipeline.** The journey numbers in memory are often @100
  and include the full pipeline. The clean comparison is the journey retrieval run at the same k on
  this same 140 sample - the obvious next step (and the reason to run on the VM, where the pipeline
  lives).
- GB only (`service=uk`); single run, no variance bands yet.

## Next

1. DONE - journey side-by-side (above): the MCP equals our pure retrieval; the apparent lead is
   gold-contaminated KG legs.
2. Run journey `all_legs` under leave-one-out (LOO) to get the honest KG contribution, then decide
   whether the KG is worth exposing to the MCP (the eval is the gate).
3. Scale from 140 to the full 1,059 active gold rows and add variance bands.
