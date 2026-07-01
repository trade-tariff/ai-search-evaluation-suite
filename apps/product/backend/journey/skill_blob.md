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
# General Interpretative Rules (GIRs 1-6)

The legal rules for classifying goods in the Harmonised System / UK tariff. Apply them in order
and stop at the first rule that resolves the choice; a lower rule never overrides a higher one.
Source: Tariff of the United Kingdom, Part Two (Rules of Interpretation).

## GIR 1 - Headings, section notes, chapter notes
Section, chapter and sub-chapter **titles are for reference only**. Classification is determined
by the **terms of the headings** and the **relative section/chapter notes**. Only if those do not
settle it do you go on to GIRs 2-6.
- **Apply when:** always - start here. The heading text and notes are the legal authority.

## GIR 2(a) - Incomplete, unfinished, unassembled
A reference to an article includes that article **incomplete or unfinished**, provided it has the
**essential character** of the finished article as presented; and includes the finished article
presented **unassembled or disassembled** (e.g. flat-pack).
- **Apply when:** the goods are incomplete/unfinished, or shipped knocked-down for assembly.

## GIR 2(b) - Mixtures and combinations
A reference to a material includes **mixtures or combinations** of that material with others, and
goods consisting **wholly or partly** of a material. If that makes more than one heading apply,
go to GIR 3.
- **Apply when:** goods are made of more than one material/substance -> usually pushes to GIR 3.

## GIR 3(a) - Most specific description
When two or more headings apply, the one giving the **most specific description** wins over a more
general one. But where each heading covers only **part** of mixed/composite goods or a set, they
are treated as **equally specific** -> go to GIR 3(b).
- **Apply when:** several headings each describe the goods. Pick the most specific; if tied on
  specificity for a mixture/set, fall through to 3(b).

## GIR 3(b) - Essential character
Mixtures, composite goods, and retail sets that 3(a) cannot resolve are classified by the
**material or component that gives them their essential character**.
- **Apply when:** composite goods or sets. Identify the dominant component - by bulk/weight, by
  value, by the part that performs the main function, or what the product is marketed around.

## GIR 3(c) - Last in numerical order
If 3(a) and 3(b) both fail, classify under the heading that comes **last in numerical order**
among those equally meriting consideration.
- **Apply when:** a genuine tie remains after 3(a) and 3(b). Pick the highest-numbered heading.

## GIR 4 - Most akin
Goods not classifiable by the above go under the heading for the goods to which they are **most
akin** (closest in nature/use).
- **Apply when:** last resort - novel goods that fit nothing cleanly. Rare; document the reasoning.

## GIR 5(a) - Fitted cases and containers
Specially shaped/fitted cases (camera, instrument, gun, jewellery cases, etc.) suitable for
long-term use and presented with their article are classified **with that article** - unless the
case gives the whole its essential character (e.g. a precious-metal case round a cheap item).
- **Apply when:** a fitted case is presented with its contents.

## GIR 5(b) - Packing materials
Packing materials/containers presented with the goods are classified **with the goods** if of a
kind normally used for them - **unless** clearly suitable for **repetitive use** (then classified
separately).
- **Apply when:** disposable packaging goes with the goods; reusable industrial packaging does not.

## GIR 6 - Subheadings
Once the heading is fixed, choose among subheadings (6-, 8-, 10-digit) by the **terms of the
subheadings** and their notes, applying GIRs 1-5 again, but **only comparing subheadings at the
same level**.
- **Apply when:** drilling from heading down to the declarable code. Compare like-for-like levels.
# Classification workflow (with tool arguments and a worked example)

The exact MCP calls for the classification method, then a worked example. All tools take
`service` (`uk` for GB, `xi` for NI) and an optional `validity_date` (YYYY-MM-DD) for historical
classification.

## The calls, in order

1. **Search candidates**
   `classification_search(query, service, limit?, expanded_query?)`
   - `query`: a clear product description. Add an `expanded_query` with synonyms if recall looks
     thin. Returns ranked candidates with `goods_nomenclature_item_id`, `goods_nomenclature_sid`,
     `description`, `declarable`, and a semantic `score`. Scores are recall evidence only.

2. **Notes for the shortlist**
   `note_mentions(goods_nomenclature_item_ids=[...] , service)` (or `goods_nomenclature_sids=[...]`)
   - Returns the section/chapter note fragments linked to those candidates. Read exclusions first
     and treat them as hard rules.

3. **Verify the tree**
   - `show_heading(heading_id, service)` - the 4-digit heading text and its children.
   - `navigate_hierarchy(code, service)` - any 4-10 digit entry; use to compare same-level siblings.
   - `lookup_commodity(commodity_code, service)` - confirm `declarable: true` and read the full
     description, section/chapter, and (handy later) `import_measures`.

4. **Resolve** with the GIRs in priority order (see `girs.md`). Cite the rule that decides it.

## Worked example: "wireless bluetooth noise-cancelling headphones" (GB)

1. **Axes**: material = plastic/electronics; form = over-ear headset, finished, retail; function =
   reproduce sound wirelessly; essential character = the headphones themselves (Bluetooth is a
   feature, not a separate article).

2. `classification_search("wireless bluetooth noise cancelling headphones", service="uk")` returns
   candidates including heading **8518** (microphones, loudspeakers, **headphones and earphones**,
   amplifiers) and tempting distractors under **8517** (telephones/other wireless network
   apparatus). The 8517 hit exists because "wireless" matches - this is exactly why you don't take
   the top search score as the answer.

3. `note_mentions` on the candidates surfaces the Chapter 85 notes. Heading 8517 is about
   transmission/reception apparatus for networks; headphones are named explicitly in 8518, so the
   more specific, correctly-described heading is 8518 (GIR 1 + GIR 3(a)).

4. Verify: `show_heading("8518", "uk")` confirms "headphones and earphones, whether or not combined
   with a microphone" sits under 8518 30. Drill in with `navigate_hierarchy` / `lookup_commodity`
   to the declarable child and check `declarable: true`. Note that a code like `8518 30 00 10`
   ("For use in civil aircraft") is a narrow end-use line - pick the subheading that matches the
   real goods, not the first declarable leaf.

5. **Resolve**: GIR 1 puts it in 8518 by the heading terms; GIR 6 takes it down to the headphones
   subheading 8518 30. Output that code, note "GIR 1 + GIR 6; 8517 ruled out - that heading is
   network transmission apparatus, headphones are named in 8518", and state you assumed standard
   consumer headphones (not aircraft end-use).

## Common traps

- **"Wireless / smart / electric" pulls in the wrong chapter.** The connectivity or power source is
  rarely the essential character. Classify the article, not its adjective.
- **Parts vs whole.** A part may have its own heading or may be classified with the machine - check
  the section/chapter notes (often a Note 2 governs parts).
- **Sets and kits.** Use GIR 3(b) essential character; don't classify the box.
- **Stopping at the first declarable leaf.** End-use lines (civil aircraft, specific industries)
  look declarable but only apply to that use. Match the trader's real goods.
