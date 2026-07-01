---
name: uk-commodity-code-classifier
description: >-
  Find the correct UK commodity code (also called HS code, HTS code, tariff code, or commodity
  classification) for a physical product, using live tariff data and the General Interpretative
  Rules. Use this whenever someone needs to classify goods for import or export - "what
  commodity code is this", "which HS code for leather hiking boots", "classify this product for
  customs", "what's the tariff code for an electric bike", "I need the 10-digit code to fill in
  my customs declaration" - or before estimating duty, because the code drives everything. It
  elicits the product facts that matter, searches candidates, checks the section and chapter
  notes, and resolves the choice with the GIRs. Prefer this over guessing a code from memory.
---

# UK commodity code classifier

Classify a product to a declarable UK commodity code by gathering evidence from the live tariff
and resolving it with the General Interpretative Rules (GIRs) - the legal rules that decide
classification. You are doing structured evidence-gathering, not pattern-matching a number from
memory: a wrong code changes the duty rate, the licences required, and the trader's legal
liability, so it has to be grounded in the actual heading text, notes, and GIRs.

## Prerequisite - the connector

This skill needs the **Trade Tariff MCP connector** (`mcp.trade-tariff.service.gov.uk`) for live
data. Confirm the tariff tools (`classification_search`, `note_mentions`, `navigate_hierarchy`,
`lookup_commodity`) are available first. If they are not, stop and tell the user to add the
connector (Claude: Settings -> Connectors; key from `https://hub.trade-tariff.service.gov.uk/`),
and do not attempt to classify from memory.

Set the `service` argument on every call: `uk` for Great Britain (default), `xi` for Northern
Ireland - the trader's destination decides which.

## The workflow

Treat search as recall (casting a wide net), and the GIRs + notes as the rules that actually
decide. Never stop at the first search hit.

### 1. Understand the product along four axes

Before searching, pin down what the goods *are*. Decompose the product (and later each candidate)
along these axes - they are the facts the GIRs turn on:

- **material** - what it is made of
- **form** - its physical form / state / how it is presented (assembled, kit, liquid, retail set)
- **function** - what it does / its intended use
- **essential character** - the single material or component that gives the whole its character
  (this decides composite goods and sets under GIR 3(b))

Ask the trader only for the axis facts you actually need to separate the live candidates - do not
run a fixed questionnaire. If their description already settles it, don't ask.

### 2. Search for candidates

Call `classification_search(query, service)` with a clear product description (you may pass an
`expanded_query` with synonyms to improve recall). This returns a ranked shortlist of candidate
goods nomenclatures with semantic scores. **Treat the scores as search evidence only, not the
answer** - the top hit is frequently not the right code.

### 3. Pull the binding notes

Call `note_mentions` with the candidate item IDs (`goods_nomenclature_item_ids`) or SIDs
(`goods_nomenclature_sids`) from step 2. This returns the section and chapter note fragments that
apply - especially **exclusion notes**. Exclusions are hard rules: if a note excludes a kind of
good from a chapter or heading, drop or demote any candidate under that excluded branch, however
high it scored.

### 4. Verify the structure

For the surviving candidates, inspect the real tariff tree rather than trusting the search text:

- `show_heading(heading_id, service)` - read the actual heading wording and its children.
- `navigate_hierarchy(code, service)` - move up/down to compare sibling subheadings at the same
  level (GIR 6 only compares subheadings at the same level).
- `lookup_commodity(commodity_code, service)` - confirm a 10-digit code is **declarable**
  (`declarable: true`) and read its description. You can only declare against a declarable code.

### 5. Resolve with the GIRs (in priority order)

Apply the rules top-down and stop at the first one that resolves the choice. Do not let a lower
rule override a higher one:

> GIR 1 (heading terms + section/chapter notes) -> GIR 2 (incomplete / unassembled; mixtures) ->
> GIR 3(a) most specific description -> GIR 3(b) essential character -> GIR 3(c) last in numerical
> order -> GIR 4 most akin -> GIR 5 (cases/packaging) -> GIR 6 (subheadings).

The full text and "how to apply" for each rule is in `references/girs.md` - read it when a choice
is non-obvious (composite goods, sets, multi-material, incomplete articles). The end-to-end method
with tool arguments and a worked example is in `references/classification-workflow.md`.

## Output

Give the trader:

- **The code**: the declarable 10-digit commodity code, formatted in pairs (e.g. `8518 30 00 10`),
  with the heading description in plain words.
- **Why**: the GIR (and any section/chapter note clause) that decided it - e.g. "GIR 1 + Chapter
  85 heading text", or "GIR 3(b): the steel frame gives it its essential character".
- **What you assumed**: the product facts you relied on. If a missing fact would change the code,
  say which and what you assumed.
- **Ruled out**: the main alternative(s) you rejected and the one-line reason, so the trader can
  sanity-check your call.

## Be honest about certainty

AI classification is strong evidence, not a binding legal ruling. For certainty - or when money or
risk is significant - the trader can apply for an **Advance Tariff Ruling (ATaR)** in the UK
(formerly BTI), which is legally binding on HMRC. Recommend it, and flag when a product genuinely
needs a human expert: borderline essential-character calls, retail sets, chemical mixtures,
parts-vs-whole questions, or anything where two headings remain credible after GIR 3.
