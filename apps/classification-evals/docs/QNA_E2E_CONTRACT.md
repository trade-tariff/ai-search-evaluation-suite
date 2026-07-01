# Q&A / E2E Contract

Last updated: 2026-06-11.

This document is the handover contract for the deployed Q&A and end-to-end
classification evaluation surface.

## Live Surfaces

- Q&A matrix: `/eval/qa-matrix`
- E2E matrix: `/eval/e2e-matrix`
- Classification trial API: `POST /api/evals/classification/trial`
- Input scoring API: `POST /api/input/score` and `POST /api/scoring/input`
- Health: `/api/health`

The active deployed process is `apps/classification-evals/backend/app.py`.
Q&A logic is implemented in `apps/product/backend/classification_core`.

## Core Tables

- `kg.e2e_eval_runs`: one row per Q&A/E2E run configuration.
- `kg.e2e_eval_results`: one row per input journey; contains retrieval rank,
  post-Q&A rank, `question_trace`, and `final_state`.
- `kg.classify_runs`: older classification matrix output from
  `run_classify_matrix.py`.
- `kg.commodity_facets`: normalized commodity facts expressed on facet keys.
  Committed session facts are persisted here once a target commodity code is
  known.

## Session Fact Persistence

Session facts are facts asserted or selected during a Q&A session. They are not
facet definitions. A facet is the normalized dimension, e.g. `material`; a
session fact is an assertion on that dimension, e.g. `material = stainless_steel`.

When a target commodity code is known, committed session facts must be written
to `kg.commodity_facets` as commodity-code facts.

Persistence shape:

- `source`: `qna_session:%`
- `commodity_code`: final/target 10-digit commodity code
- `facet_key`: normalized session slot/facet key
- `facet_value`: normalized selected answer
- `authority_tier`: follows the underlying evidence source
- `use_scopes`: usually `classification`, `qa`, `audit`
- `evidence_roles`: inferred from the fact key, e.g. `material_composition`
- `provenance`: run/session/query/question/source metadata

Authority rule:

- Trader/human/ad-hoc assertion: low authority, default tier `8`.
- ATAR-backed oracle/session fact: tier `5`, matching ATAR-derived extracted
  product facts.
- The ATAR ruling or rationale edge itself remains tier `2`; the Q&A-derived
  fact is not the ruling itself.
- Legal note facts/rules remain tier `1`.

Verification rows on 2026-06-11:

- `id=38739`: low-authority verification session fact, tier `8`.
- `id=38740`: ATAR/oracle-style authoritative session fact, tier `5`.

## Result Trace Shape

`kg.e2e_eval_results.question_trace` is a JSON array. Each item normally
contains:

- `round`
- `question`
- `mode` / `requested_mode`
- `source`
- `facet_key` or `signal_key`
- `options`
- `answer`
- `answer_meta`
- `answer_debug`
- `active_before`

`kg.e2e_eval_results.final_state` contains strategy-specific state:

- `qa_state`: active/in-scope codes and Q&A state where available
- `policy_eval`: hard-prune, conservative-prune, and score-only summaries
- `staging_debug`: LLM candidate-selection/debug blocks for staging eliminate
- `fallback_to_retrieval_rounds`: count of rounds that fell back to retrieval order
- `persisted_session_facts`: persistence summary from `kg.commodity_facets`
- `leg_counts`: retrieval leg counts

## Strategies

- `facet_rules`: deterministic facet bucket questions and pruning.
- `facet_rules_llm_wording`: same buckets as `facet_rules`, but with LLM wording.
- `llm_generated`: LLM-generated question flow.
- `staging_eliminate`: fixed retrieval shortlist; LLM eliminates/ranks
  candidates without re-retrieving.

Current interpretation:

- Hard deterministic facet pruning is brittle.
- `staging_eliminate` with softer elimination has produced the best current
  Q&A evidence.
- High-reasoning prompts can become too conservative; track fallback rows before
  trusting top-1 results.

## Failure Modes

- No retrieval candidates.
- Gold code not in initial retrieval shortlist.
- Provider disabled or missing key for provider-backed modes.
- Cost cap exceeded.
- Malformed LLM/tool output.
- Fallback-to-retrieval-order candidate selection.
- No discriminating facet available.

## Promotion Criteria

Do not promote a Q&A mode or prompt from top-1 alone. Check:

- initial gold-in-retrieval
- gold kept after Q&A
- top-1 after Q&A
- average post-QA rank
- average active set size
- provider calls and errors
- fallback-to-retrieval rows
- trace/debug samples for false eliminations

## Spend Rules

Provider-backed runs require explicit spend enablement:

- request `allow_spend: true` where supported
- relevant provider key present
- server cost/session/concurrency caps satisfied

No-spend surfaces:

- `POST /api/input/score` with default `baseline_fts_only`
- deterministic DB reads/matrices
- DB-backed provenance and trace inspection
