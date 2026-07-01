# Trader Journey - end-to-end UI test

Date: 2026-06-16
Target: the deployed standalone journey on the EC2 VM - https://journey.18.175.148.215.sslip.io/
(Caddy basic-auth, user `tariff`). Driven with Playwright against the public URL.
Model: gpt-5.5, high reasoning effort, via the OpenAI /v1/responses API (~3 min per
classify/answer turn - accepted). A fast nano pass was used to walk the later stages.

## Result: PASS - core journey works end to end in the browser

Journey-only build is live (single "Trader Journey" tab). Classify -> Value -> Duty flows
in the UI, carrying state forward, with gpt-5.5 authoring the clarifying question and the
transparency panel rendering.

## Stage by stage

1. Landing - single Trader Journey tab; 5-step stepper (Classify -> Value -> Duty details
   -> Import costs -> Declare); example chips. [e2e-01]
2. Classify - typed "Chocolate protein powder"; streamed progress ("Understanding your
   description...", "Searching...") - not a frozen spinner. [e2e-02]
3. Clarifying question (gpt-5.5) - "A quick question about your goods", 100 candidate codes
   considered; an LLM-authored question grounded in the shortlist with real options. [e2e-03]
4. Transparency panel "What & why" - collapsed by default; shows the eliminate-strategy
   explanation, the extracted user-asserted facts, and (after answering) the Q&A state
   ("Conversation so far"). See Known issues for the prompt/trace sub-sections.
5. Classified - answered the flavour question -> committed to 1806907090 ("Preparations
   containing cocoa, for making beverages") as BEST MATCH, with a ranked alternates list.
   Classify step marked complete. [e2e-04]
6. Value - "I know the customs value" -> GBP 1000 -> review -> calculate. Commodity + value
   carried into Duty. [e2e-05]
7. Duty details - carries Commodity 1806907090 + Customs value GBP 1000 + Origin CN; walks
   import date -> country of origin -> proof of origin -> additional code -> review, with GB
   destination + 20% VAT pre-filled. [e2e-06/07]

## Downstream computations (verified via API on loopback - all HTTP 200 with real data)

- /api/duty            -> duty + VAT breakdown
- /api/valuation       -> customs value + method
- /api/landed          -> VAT, total landed cost
- /api/declaration     -> CDS box values, required document codes, next steps
- /api/commodity/{code}/hydrate -> external GOV.UK fetch works (~0.7s)

## Fixed during this test

- SPA catch-all now returns 404 for unmatched /api + /eval (was serving index.html, which
  caused "Unexpected token '<'" JSON-parse errors in the console).
- ClassifyTurn schema now declares eliminate_trace + survivors_all (were dropped on
  serialization).

## Known issues / follow-ups

- Transparency panel: FIXED. Root cause was the FRONTEND defaulting to strategy=converge
  (configForProcessMode in ClassifyStage.tsx), which routes to classify_step and never runs
  _llm_eliminate - so no prompt was captured and no eliminate_trace produced, regardless of
  backend defaults. The frontend now defaults to strategy=eliminate + model gpt-5.5; the panel
  shows the actual ELIMINATION-round prompt (system+user), the survivors/ruled-out trace, the
  extracted facts, and the Q&A state. Verified in the UI (gpt-5.5, ~130s turn). Also fixed:
  ClassifyTurn schema now carries eliminate_trace; prompt_user capped to 3KB in the streamed
  debug to keep the panel readable + the SSE lean.
- Console 404s on load: the app shell still fetches workbench endpoints (/api/config,
  /api/prompts, /api/atar/drafts) the journey-only backend does not serve. Harmless,
  devtools-only.
- gpt-5.5 latency ~3 min per turn (high effort via responses API). Accepted.
- Duty stage is a multi-sub-step gated form (fine for users; fiddly for headless automation).

## How to run

Open https://journey.18.175.148.215.sslip.io/ (basic-auth user `tariff`; password in the
local CREDENTIALS.local.md). Type a product or pick an example chip -> answer the clarifying
question -> proceed through Value / Duty details / Import costs / Declare.
