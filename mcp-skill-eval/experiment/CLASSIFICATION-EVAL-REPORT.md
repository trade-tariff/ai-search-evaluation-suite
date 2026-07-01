# UK Commodity Classification: experimental pipeline vs MCP+skill

**Evaluation report - 2026-06-25.** Run on the classification-evals EC2 VM (gpt-5.5 + journey
harness) and locally (Claude arm). Sample = 40 gold rows. All artifacts in
`/opt/ai-search-evaluation-suite-journey/mcp-skill-eval/experiment/`.

---

## 1. Executive summary

We compared the experimental KG/Q&A classification pipeline (the "journey" engine) against a
**vanilla LLM + the OTT MCP connector + a classification skill**, on the same 40 hard gold codes,
on one metric that matters: does the agent land the **exact declarable 10-digit commodity code**
at top-1 and within top-10.

**Headline: the MCP+skill approach roughly doubles the best pipeline config.**

| Approach | top-1 | top-10 |
|---|---:|---:|
| **MCP + skill (gpt-5.5)** | **65.0%** | **92.5%** |
| **MCP + skill (Claude Opus)** | **52.5%** | **92.5%** |
| Best pipeline config (converge + KG) | 32.5% | 37.5% |

The win is **model-robust** (gpt-5.5 and Claude both hit 92.5% top-10; both far above the pipeline)
and **consistent** with an earlier independent 210-row run (MCP+skill ~55% top-1). It is also
operationally competitive: the MCP arm runs in **~1 Q&A round, ~68s/search** - as fast as the
cheapest pipeline config, and **~8x faster** than the pipeline's eliminate strategy.

**Recommendation:** the connector+skill architecture (the original Slack thesis) is validated -
prioritise it. Within the pipeline, **drop the eliminate strategy** (dominated on every axis) and
treat **converge+KG** as the lean fallback.

---

## 2. The question

The programme had three stages:
1. **Retrieval** - get the right declarable code into the candidate set. Already strong:
   **96.3% recall@100** (declarable) on the experimental retrieval. Not re-tested here.
2. **Q&A loop** - from those candidates, which strategy best (a) ranks the right code top-1 and
   (b) keeps it in top-10 (results-page). This is what we swept.
3. **vs vanilla LLM + MCP** - how does an agent with just the connector + a skill compare to the
   best experimental config.

This report covers stages 2 and 3.

---

## 3. Method

- **Sample:** 40 gold rows = **40 distinct declarable codes**, persona-rotated across the 7 input
  personas, drawn from the ATAR-derived `kg.eval_gold` set. ATAR codes are specific and sometimes
  genuinely debatable, so this is a **hard** set - absolute numbers understate real-world accuracy.
- **Metric:** exact match on the **declarable 10-digit code**; top-1 and top-10. Heading-level
  match intentionally ignored (per the brief).
- **Arms (8):**
  - 6 **journey** configs = {converge, eliminate} x {no-KG, +KG, +KG+rule_reasoning} (the no-KG
    rule_reasoning cells were dropped for budget).
  - 2 **MCP** arms = gpt-5.5 and Claude Opus, each = the LLM driving the `uk-commodity-code-classifier`
    skill against the live OTT MCP, emitting a ranked top-10.
- **Oracle:** clarifying questions answered by gpt-5-mini from the source ATAR text, constrained to
  never reveal a code. Identical across all arms.
- **Where:** journey configs + the gpt-5.5 MCP arm on the EC2 (the only host with gpt-5.5); the
  Claude arm locally via `claude -p` (no Anthropic key on the box). Same skill, MCP, oracle, sample,
  scoring across both.
- **Budget:** a $200 cost-guard on the journey est_cost; actual spend ~$212.

---

## 4. Results

### 4.1 Accuracy (n=40)

| Arm | top-1 | top-10 | with_code |
|---|---:|---:|---:|
| **MCP+skill - gpt-5.5** | **65.0%** | **92.5%** | 38/40 |
| **MCP+skill - Claude Opus** | **52.5%** | **92.5%** | 40/40 |
| journey - converge - +KG - rule_reasoning | 32.5% | 37.5% | 40 |
| journey - converge - +KG | 32.5% | 32.5% | 40 |
| journey - converge - no-KG | 22.5% | 27.5% | 40 |
| journey - eliminate - no-KG | 10.0% | 40.0% | 40 |
| journey - eliminate - +KG | 15.0% | 37.5% | 40 |
| journey - eliminate - +KG - rule_reasoning | 12.5% | 37.5% | 40 |

### 4.2 Operations (Q&A rounds, latency, cost per search)

| Arm | Q&A rounds | duration/search | cost/search* |
|---|---:|---:|---:|
| MCP+skill - gpt-5.5 | 0.9 | 68s | ~$0.50 |
| MCP+skill - Claude Opus | ~1 (not logged) | ~75s | local Claude session |
| converge - no-KG | 1.0 | 62s | ~$0.25 |
| converge - +KG | 1.1 | 63s | ~$0.27 |
| converge - +KG - rule_reasoning | 1.1 | 67s | ~$0.27 |
| eliminate - no-KG | 4.9 | 530s (~9 min) | ~$1.35 |
| eliminate - +KG | 4.8 | 533s | ~$1.32 |
| eliminate - +KG - rule_reasoning | 4.8 | 514s | ~$1.33 |

\* The journey harness `est_cost_usd` under-prices gpt-5.5 (cost.py uses ~$1.25/$10 vs real $5/$30);
figures are scaled ~12x to match the ~$212 actual spend the $200 guard operated against. The MCP
$0.50 is a direct gpt-5.5 token estimate. All cost figures are +/-30%.

---

## 5. Findings

1. **MCP+skill ~2x the best pipeline config.** top-1 52-65% vs converge+KG's 32.5%; top-10 92.5% vs
   37.5% (~2.5x). The gap dwarfs the n=40 sampling noise (~+/-15% per cell).
2. **Model-robust.** gpt-5.5 and Claude both hit 92.5% top-10; gpt-5.5 edges Claude on exact top-1
   (65% vs 52.5%, a 5-row gap = within noise). So the result is the *architecture*, not a lucky
   model. (gpt-5.5's 2 no-code rows count as misses, so its true ceiling may be a touch higher.)
3. **Consistent with prior work.** An earlier independent 210-row run had MCP+skill at ~55% top-1 -
   same ballpark, different sample.
4. **eliminate is dominated.** ~5 Q&A rounds, ~9 min/search, ~$1.3, *and* the worst top-1 (10-15%).
   It re-justifies all ~100 candidates every round. No reason to keep it.
5. **KG helps converge** (+10pts top-1: 22.5 -> 32.5); rule_reasoning adds a little top-10. KG did
   **not** rescue eliminate.
6. **The bottleneck is the Q&A/ranking, not retrieval.** Retrieval already puts the gold in the
   candidate set 96.3% of the time @100; the pipeline's converge loop only converts that to ~33%
   top-10, whereas the MCP agent converts to 92.5%. The agentic loop is the lever.

---

## 6. Why the MCP+skill wins (interpretation)

The pipeline's Q&A loop is a constrained re-rank/converge over a frozen candidate set. The MCP arm
is a **free agent**: it issues its own `classification_search`, drills the hierarchy
(`show_heading` / `navigate_hierarchy` / `lookup_commodity`), confirms declarability, and resolves
by the **GIRs** that the skill spells out - ~20 tool calls and multi-step reasoning per product.
That agency + the explicit GIR method is what converts good retrieval into a correct, well-ranked
answer. The connector supplies the data; the skill supplies the method; the model supplies the
reasoning.

---

## 7. Caveats and limitations

- **n=40, single run.** Directional, not publishable. Per-cell CIs ~+/-15%; no variance bands.
  Scale to 150-1,059 before quoting externally.
- **Hard, debatable gold.** ATAR codes; some "misses" are defensible alternatives (e.g. an LLM
  picking 8484 "mechanical seals" where the ATAR recorded 7326 "other steel articles").
- **Cost is estimated**, not invoiced - the harness under-prices gpt-5.5; the ~12x scaling is
  calibrated to the budget, not a billing export.
- **Claude per-row ops not logged** (the `claude -p` arm recorded only codes) - rounds/cost are
  estimates.
- **Oracle is a simulation** of a cooperative importer; a real user is noisier.
- **note_mentions was unavailable** for both arms (see 8.1), so the MCP arm scored *without* the
  notes tool - i.e. this is a floor for the MCP approach, not a ceiling.

---

## 8. Infrastructure issues surfaced

### 8.1 MCP `note_mentions` returns 422 for every commodity
The notes tool POSTs a valid query to the backend `/knowledge_graph/queries`, but no subject
resolves -> the `tariff_knowledge_*` node/edge tables are not populated on the prod backend the MCP
points at. MCP code and backend code are both correct; the KG **data** is missing on prod. Full RCA
+ reproduction + fixes in `MCP-note_mentions-BUG.md`. (Independently confirmed by codex.)

### 8.2 "Getting more detail" hangs in the journey UI
`POST /api/hydration/candidates` hydrates up to all ~80 candidates in a **sequential** loop (one
gpt-5-nano call each) in a blocking handler - no concurrency / cap / timeout. ~80 back-to-back LLM
calls = minutes; the "73/80" bar is a fake timer that parks near the top. Fix = parallelise + cap to
the top ~20-30. Detail in the `project_journey_hydration_hang` memo.

### 8.3 MCP WAF is hostile to agent/batch traffic
The MCP sits behind CloudFront + WAF. A default `curl` User-Agent gets `403 Request blocked`
(needs a browser UA), and bursting at concurrency ~6 trips a rate-based **challenge** (HTTP 202,
silent JS proof-of-browser) that non-browser clients can't pass; it clears after ~300s idle.
Practical limit for programmatic callers: **concurrency <=2 with pacing + backoff**. Ironic for a
service meant to serve AI agents - worth flagging to the MCP team.

---

## 9. Recommendations

1. **Back the connector+skill direction.** It is the strongest performer and the original thesis;
   the data supports prioritising it (OTT as a marketplace connector + a small set of skills).
2. **Drop the eliminate strategy** from the pipeline - dominated on accuracy, latency, and cost.
3. **If a self-hosted pipeline is retained, use converge+KG** as the lean engine (~1 round, ~63s).
4. **Harden the finding before external use:** scale to 150 then the full 1,059; add variance bands;
   log the Claude arm's ops; and once `note_mentions` is fixed, re-run the MCP arm **with** notes to
   measure the KG's lift on the MCP path (this run is the no-notes floor).
5. **Fix the three infra issues** above - note_mentions data, the hydration loop, and the WAF
   posture for agent traffic.

---

## 10. Reproduction and artifacts

On the EC2 at `/opt/ai-search-evaluation-suite-journey/mcp-skill-eval/experiment/`:
- `sample40.json` / `arm_sample.json` - the 40-row sample (+ oracle text).
- `run_experiment.sh` - the journey-config sweep (Phase 1) + MCP arm + scorer.
- `arm_openai_top10.py` - the gpt-5.5 MCP agent; `run_claude_mcp.sh` (local) - the Claude arm.
- `score_experiment.py` - scorer; `EXPERIMENT-RESULTS.md`, `score_summary.json` - outputs.
- Journey per-row rows in `kg.classify_runs` (filter `run_label like 'exp_%'`).
- Raw per-id outputs: `mcp_base/` (gpt-5.5), and `/tmp/mcp_claude/` locally (Claude).
- Related: `MCP-note_mentions-BUG.md`, `EXPERIMENT-PLAN.md`, `customs-skills/` (the skills).
