# End-to-end classification eval - design

Status: DESIGN ONLY (not run). For sign-off before spending agent tokens.

## Why this replaces the recall benchmark

Single-call recall@k measures a one-shot retriever. The MCP+skill setup is not one-shot: the agent
reformulates queries, fires `classification_search` several times, pulls `note_mentions`, drills the
hierarchy, and adds/drops candidates by reasoning. So recall of any one call is only a floor. What
matters is the **final code the loop commits to** - top-1 / top-3 - which is the same number your
pipeline is judged on. This eval measures that.

A useful side-property: the MCP path never touches the journey `kg` facts/edges, so it is
**LOO-clean by construction** - none of the gold-memorisation that inflated the retrieval matrix's
`all_legs` configs can happen here.

## System under test

Agent = the `uk-commodity-code-classifier` SKILL.md + the OTT MCP tools, nothing else. The loop being
evaluated IS the skill (search -> note_mentions -> navigate/show_heading/lookup_commodity ->
GIR-resolve -> emit final code + GIR cited + assumptions). No local DB, no other tools.

## The crux: clarifying questions

The skill is built to ask the trader for missing facts (material/form/use), which is most of its
value on vague/branded inputs. Batch eval has no human, so:

- **Oracle mode (default).** A cheap separate model plays the trader, answering only from the
  **source ATAR text** behind each gold row (the 7 personas are all paraphrases of one ATAR; the
  verbatim ATAR description is the ground-truth fact source, joinable by `source_id`). Oracle rules:
  answer only what is asked, only from the ATAR facts, never state or hint the code, say "not
  specified" when absent. This mirrors your `qa_loop.py` oracle and is the realistic measure.
- **No-ask mode (secondary).** Agent must commit from the query alone - a lower bound that isolates
  single-turn performance.
- Cap questions per row (e.g. <= 5) to match the deployed max-questions config.

## The two arms (the comparison you asked for)

Identical skill text, identical MCP, identical oracle, identical scoring. Only the driver model
differs:

- **Arm A - gpt-5.5** (OpenAI, your key). MCP's 13 tools wrapped as OpenAI function schemas; a thin
  dispatcher forwards each tool call to the MCP JSON-RPC and returns the result. Model-matched to the
  journey pipeline.
- **Arm B - Claude** (this harness / Agent SDK), MCP attached natively.

Report A vs B on every metric, including tool-use behaviour (how each model actually drives the loop).

## Baselines for context

- **Floor:** `classification_search` single-call recall@100, approximated by unioning 2x limit-50
  calls (the tool caps at 50/call) on the same sample. Shows how much the loop adds over raw search.
- **Optional 3rd arm:** the journey app's own end-to-end loop (`qa_loop.py` / deployed eliminate) on
  the same sample + oracle, for skill-vs-your-pipeline at the task level. Note: your stored top-1 was
  withdrawn, so this needs its own clean run to be an honest baseline - flagged, not assumed.

## Sample

Stratified by persona (7). Quick = 49 (7/persona); fuller = 210 (30/persona). Same fixed rows (by
id) for both arms, for reproducibility and paired comparison. Source = `kg.eval_gold` (1,059 active).

## Scoring (programmatic except where noted)

- **top1_exact**, **top1_d8**, **top1_d6**, **top1_d4** - final code vs gold prefix at each level.
- **top3_*** - if the skill emits ranked alternatives.
- **defensible_alt** - when top1 != gold, a judge model (different from both arms, or manual) flags
  whether the agent's code is a defensible alternative (e.g. mechanical seal `8484` vs gold `7326`).
  Reported separately so "disagrees with gold" is not conflated with "wrong".
- **GIR sanity** - did it cite a plausible rule for its pick (cheap judge or manual spot-check).
- **efficiency** - mcp_calls, llm_turns, oracle_questions, wall_s, tokens, est_cost per row.

## Harness mechanics

- One run per (gold row, arm), fresh context. System = SKILL.md; user = `gold.query`; tools = MCP
  (Bearer token via `.env` client_credentials, refreshed ~50 min).
- Arm B (Claude): Task subagents with the MCP server configured, or the Agent SDK.
- Arm A (gpt-5.5): OpenAI Responses API with `tools` = the 13 MCP tool schemas; dispatcher bridges
  tool-calls to MCP JSON-RPC.
- Oracle: a cheap model (e.g. gpt-5-mini / Haiku) holding the ATAR text, engaged only when the agent
  asks a question; every Q/A logged for leakage audit.
- Read-only. Nothing written to any DB. Outputs: per-row transcript + results JSON + an aggregate
  table (per arm, per persona).

## Threats to validity

- **Oracle leakage** - strict constraints + full Q/A logs; spot-check that it never reveals the code.
- **Gold debatability** - handled by the defensible_alt track.
- **Tool-binding asymmetry** (OpenAI function-calls vs Claude native MCP) - keep skill text identical;
  only the binding differs; tool-call counts surface any divergence.
- **gpt-5.5 latency/cost** (~1-2 min/turn observed) - start with the 49-sample.
- **Judge bias** on defensible_alt - judge model differs from both arms, or do it by hand.

## Cost / time (rough, confirm before running)

49 rows x 2 arms x (~3-6 LLM turns + ~4-10 MCP calls + oracle turns). gpt-5.5 is the slow/expensive
arm (~1-2 min/row); Claude faster. Quick run ~1-2h wall, low-thousands of LLM calls total, modest
token spend. The 210-sample is ~4x that.

## What it answers

1. How the MCP+skill actually classifies end-to-end (final top-1), vs the misleading single-call
   recall number.
2. **gpt-5.5 vs Claude** as the driver - accuracy, cost, latency, and how each one drives the loop.
3. Whether the skill loop closes the gap that raw retrieval recall showed - i.e. is the
   connector+skill architecture good enough to lean on.

## Open questions for sign-off

- Oracle on by default (realistic) or run both oracle + no-ask?
- Sample size: 49 to start, or straight to 210?
- Include the optional journey-app 3rd arm now, or just the two model arms first?
- Judge model for defensible_alt (and is that track wanted v1, or just raw top-1)?
