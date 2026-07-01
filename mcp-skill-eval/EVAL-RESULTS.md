# End-to-end classification eval - results

gpt-5.5 vs Claude Opus 4.8, both driving the `uk-commodity-code-classifier` skill over the OTT MCP.
210 gold rows (116 distinct codes, 30/persona x 7), identical sample, oracle answering clarifying
questions from the source ATAR text. Final-answer top-1 (not single-call recall).

## Headline

| Arm | top1 exact (d10) | d8 | d6 | heading (d4) | oracle Qs/row |
|---|---|---|---|---|---|
| gpt-5.5 (EC2, default reasoning) | **0.548** | 0.576 | 0.638 | **0.700** | 0.7 |
| Claude Opus 4.8 (local, no extended thinking) | **0.548** | 0.571 | 0.643 | **0.700** | 0.8 |

**Statistically dead even** - both ~55% exact 10-digit, ~70% correct heading. Inter-model agreement:
**75%** identical 10-digit code, **86%** same heading. Each had **32/210 right-heading-wrong-leaf**
(some are defensible alternatives to the ATAR gold, not errors - see caveats).

### top-1 exact by persona

| persona | gpt-5.5 | Opus 4.8 |
|---|---|---|
| emu_generic | 0.47 | 0.53 |
| emu_ordinary | 0.57 | 0.57 |
| emu_specific | 0.57 | 0.60 |
| naive_branded | 0.50 | 0.47 |
| naive_specific | 0.57 | 0.53 |
| naive_vague | 0.53 | 0.57 |
| original | 0.63 | 0.57 |

## What it means

1. **The agentic loop is the whole game - recall is just a floor.** Single-call
   `classification_search` got ~0.12 exact in the top-10 (see RESULTS.md). The end-to-end skill
   loop (search -> notes/hierarchy -> GIR-resolve, with Q&A) gets **~0.55 exact top-1** - roughly
   **4.5x** the raw-retrieval hit rate. This is the case for the connector+skill architecture, and
   it confirms that single-shot retrieval recall under-measures an MCP+LLM setup.

2. **Model choice barely matters here; the skill does the work.** gpt-5.5 and Opus 4.8 are
   indistinguishable on accuracy. So the differentiator is the skill + the live data, not the
   model - which is good news for portability across Claude/OpenAI marketplaces.

3. **Opus matched a reasoning model while handicapped.** gpt-5.5 ran with its default reasoning;
   Opus 4.8 ran with **no extended thinking** (deliberate choice) and still tied. Enabling thinking
   on the Claude side would likely push it ahead, but wasn't needed for parity.

## Caveats (don't over-read)

- **Effort asymmetry (known):** gpt-5.5 reasons by default (couldn't pass `reasoning_effort` with
  function tools on chat/completions, so it ran at the API default ~medium); Opus 4.8 had no
  thinking budget. So this is "gpt-5.5-with-reasoning vs Opus-without" - and they tied.
- **Hard, debatable gold:** ATAR-derived codes; the 32 right-heading-wrong-leaf per arm include
  genuine alternatives (e.g. mechanical seal 8484 vs gold 7326). Raw top-1 therefore understates
  both systems; a defensible-alt judge pass would lift both.
- **`note_mentions` was down** (backend 422 on `/uk/api/v2/knowledge_graph/queries`) for the whole
  run - both arms fell back to the hierarchy. Fixing it may lift both. Flag for the MCP team.
- **Runtime difference:** Arm A = OpenAI function-calling script on the EC2; Arm B = `claude -p` /
  workflow agents. Same skill, MCP, oracle, sample, scoring - only the model + its agent runtime
  differ. MCP call counts captured for gpt-5.5 (~14/row) but not the Claude arm.
- Single run, 210 rows; no variance bands yet.

## Configs

- **Arm A:** `gpt-5.5`, default reasoning, OpenAI `/v1/chat/completions` + function tools; MCP via
  in-process JSON-RPC dispatch; 4 sharded workers on the EC2 (`journey-app` container).
- **Arm B:** `claude-opus-4-8` (1M ctx), no extended thinking, via `claude -p` (no API key - reuses
  the logged-in CLI); MCP + oracle via bash wrappers; PAR=6 locally.
- Shared: `uk-commodity-code-classifier` SKILL.md + references; oracle = `gpt-5-mini` answering from
  the source ATAR text; `submit_classification` as the final answer; scoring at 10/8/6/4-digit.

## Files (this dir, on the EC2 eval home)

`eval_sample210.json` (gold + oracle text) | `arm_a_results/` + `arm_a_full.json` (gpt-5.5) |
`arm_b_results/` (Opus) | `score.py` + `score_summary.json` | `arm_openai.py` (Arm A harness) |
`run_armB.sh` (Arm B claude -p runner) | `eval/` (mcp_call.sh, oracle) | `EVAL-DESIGN.md` |
`customs-skills/` (the skills under test).
