Right, had a think and a play over this. Where I've landed, plus what I built.

**Architecture - I reckon we're all saying the same thing**
The MCP is the connector: it's the access to the data. Skills are the repeatable know-how on top -
the .md that tells the model which tool to call, in what order, and how to read the answer the same
way every time. Not competing, just layers. Maps onto Rob's two segments: Segment 1 (big traders,
own systems) mostly want the connector; Segment 2 (the nervous one-off importer on ChatGPT) needs
the skills, because they don't know what to ask or how to read a tariff schedule. So a guide-me
skill sits on top for Segment 2 and routes them to the right one.

**Eval - end-to-end, result's in**
Single-call retrieval recall was the wrong yardstick (the agent fires several searches and curates
the shortlist itself). So I ran the classifier skill end-to-end over 210 gold rows - final top-1,
two models driving the same skill+MCP. Result: **~55% exact 10-digit, ~70% correct heading**, and
**gpt-5.5 vs Claude Opus 4.8 are dead even** (both 0.548 exact). Two things matter: (1) the agentic
loop is ~4.5x the raw-retrieval hit rate (single-call was ~12%) - the skill is the value, not the
search; (2) accuracy is basically model-agnostic, which is ideal for shipping across both
Anthropic/OpenAI marketplaces. Caveats: hard ATAR gold and ~15% of "misses" are defensible
alternatives, so true accuracy is higher; and Opus matched gpt-5.5 while running with no thinking
budget. Full writeup + raw data on the evals box: mcp-skill-eval/EVAL-RESULTS.md.

**I had a go - 3 skills**
Built as proper Agent Skills (SKILL.md), connector-only, distilled straight from our journey prompts
(the GIR decomposition + the duty/landed-cost logic) so they're grounded, not made up:
- guide-me - the Segment-2 front door
- uk-commodity-code-classifier - GIR-led: search -> section/chapter notes -> verify in the hierarchy
  -> resolve by GIR priority, and it cites the rule that decided it
- duty-and-tax-estimator - duty + import VAT + landed cost off the live measures, with the working shown
Verified the MCP live while building (13 tools; classified "wireless headphones" -> 8518 cleanly).
They're in customs-skills/.

**The 15-skill list - honest triage**
OTT genuinely backs about 5 (classifier, duty, FTA/rules-of-origin, compliance-checker,
doc-checklist) plus guide-me. The rest (incoterms, invoice/packing builders, IOR explainer, hold
advisor...) are guidance or templates that any decent model already gives - fine to ship, but let's
not sell them as data-driven. Two I'd flag hard: sanctions-screener and dangerous-goods-classifier
are real compliance products. An LLM that gets those wrong is worse than nothing because it creates
false confidence. Only build them with proper datasets and a loud "indicative only, see a
professional". Full table in CATALOGUE.md.

**On Will's productionisation list (agree, couple of notes)**
- Hybrid search: looks like it's already in - classification_search advertises hybrid semantic
  retrieval, and it's the same backend as staging's OpenSearch+pgvector RRF. Worth confirming before
  we rebuild it.
- KG: our journey kg schema (GIRs, facets, edges) is the asset to expose - I'd propose get_girs and
  get_kg_edges(codes, scope) tools. And the eval harness above is exactly how we prove whether the KG
  actually lifts accuracy. It's a quality lever and costs almost nothing in tokens, so worth doing if
  it moves the number.
- Dynamic registration: /oauth/register currently returns "automatic registration not supported", so
  RFC 7591 is the fix - then users don't have to hand-paste a client id/secret.

Next: get the eval number, then eval-gate the three skills (with-skill vs baseline) before anything
goes near a marketplace. Shout if you want the skills dropped into a shared repo.
