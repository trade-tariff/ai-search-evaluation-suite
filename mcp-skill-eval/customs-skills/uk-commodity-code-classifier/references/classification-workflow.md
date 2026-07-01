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
