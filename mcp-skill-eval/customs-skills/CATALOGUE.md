# Customs Skills catalogue - triage

The 15 skills proposed in the thread, triaged by **trader anxiety**, **which segment they serve**,
and - the key question - **whether OTT tariff data actually backs them**. The honest line to take
back to the chat: the OTT-backed skills are the defensible, differentiated ones. The rest are
guidance wrappers any general model already gives - useful, but don't market them as data-driven,
and don't let the two genuinely risky ones (sanctions, dangerous goods) masquerade as authoritative.

## Segments (Rob's framing)

- **Segment 1 - big traders, direct API in their own systems.** The MCP connector is the value;
  they bring their own analysis. Skills are largely irrelevant here.
- **Segment 2 - infrequent users doing lookups via ChatGPT/Claude.** They don't know what to ask
  or how to read a tariff schedule. **Skills are the value**, fronted by `guide-me`.

## Triage table

| # | Skill | Trader anxiety | OTT-backed? | Tools / data source | Tier |
|---|---|---|---|---|---|
| 0 | **guide-me** | "where do I start" | Orchestration | routes to the others | **Built** |
| 1 | **uk-commodity-code-classifier** | "what is it / am I declaring it right" | Yes | `classification_search`, `note_mentions`, `navigate_hierarchy`, `lookup_commodity` | **Built** |
| 2 | **duty-and-tax-estimator** | "what will it cost" | Yes | `lookup_commodity`, `list_geographical_areas`, `rules_of_origin`, `search_quotas`, `list_exchange_rates` | **Built** |
| 3 | fta-advisor / rules-of-origin | "can I pay less duty" | Yes | `rules_of_origin` + preference measures | T1 - build next |
| 4 | trade-compliance-checker | "am I allowed to ship this" | Partial | `lookup_commodity` measures + footnotes + `list_certificate_types`; **+ external** control lists | T1 - build next (scope to tariff-visible controls) |
| 5 | shipment-document-checklist | "what paperwork stops a hold" | Partial | measure conditions + `list_certificate_types` + general knowledge | T1 - build next |
| 6 | certificate-of-origin-guide | "what proof of origin" | Partial | overlaps `rules_of_origin`; issuance is chamber-of-commerce knowledge | T2 |
| 7 | incoterms-advisor | "who's liable for what" | No | static knowledge (Incoterms 2020) | T2 |
| 8 | commercial-invoice-builder | "will my invoice clear" | No | document generation | T2 |
| 9 | packing-list-builder | "is my packing list right" | No | document generation | T2 |
| 10 | cargo-insurance-advisor | "am I covered" | No | guidance | T2 |
| 11 | customs-hold-advisor | "it's stuck, now what" | No | guidance | T2 |
| 12 | importer-of-record-explainer | "what am I liable for" | No | guidance | T2 |
| 13 | broker-brief-generator | "brief my broker" | No (glue) | orchestrates outputs of 1-5 | T2 - high Segment-2 value |
| 14 | **sanctions-screener** | "is this party banned" | No | needs OFAC / UK OTSI / EU lists - a real screening data product | T3 - do NOT ship as OTT-backed |
| 15 | **dangerous-goods-classifier** | "is it hazmat" | No | needs IATA / IMDG / ADR datasets | T3 - do NOT ship as OTT-backed |

## Tiers

- **T1 (OTT-backed, build):** 1, 2, 3, 4, 5 (+ guide-me). These are the differentiated set - the
  live tariff is the moat. Skills 4 and 5 are "partial": scope them to what the tariff actually
  shows (measure conditions, required certificates/licences, footnotes) and hand off to a human or
  an external source for the rest.
- **T2 (guidance / document generation, no tariff dependency):** 6-13. Genuinely useful for
  Segment 2, but any capable model does most of this without OTT. Ship them, but be clear they are
  guidance/templates, and lean on the audit trail and the plain-English handholding as the value,
  not data exclusivity. `broker-brief-generator` is the best of these - it stitches the T1 outputs
  into one document to hand a broker.
- **T3 (needs a separate compliance data source - handle with care):** 14, 15. Sanctions screening
  and dangerous-goods classification are real regulated products. An LLM "screen" that is wrong is
  worse than none, because it creates false confidence. If built at all, they must (a) use proper
  list/dataset sources, (b) be explicit they are indicative only, and (c) always route to a
  licensed professional. Do not present them as OTT-backed.

## The throughline (applies to every skill)

1. Translate broker/forwarder jargon into plain English.
2. Always tell the trader the next action.
3. Be honest when a question needs a licensed professional or a binding ruling, rather than an AI.

## Publish path

1. **Bundle.** Keep the T1 skills together as a "Customs / Trade Tariff" skill set, all declaring
   the MCP-connector dependency, fronted by `guide-me`.
2. **Dev Portal.** List the skill set alongside the MCP docs on the Trade Tariff developer portal
   (`hub.`), so anyone adding the connector sees the matching skills.
3. **Anthropic (and OpenAI) marketplace.** Publish the bundle as installable skills; the connector
   provides access, the skills provide the know-how. Package with `skill-creator`'s
   `package_skill.py` to produce `.skill` files.
4. **Eval-gate before promoting.** Run each T1 skill through the skill-creator eval loop (realistic
   trader prompts, with-skill vs baseline) before it goes on a marketplace - same discipline as
   benchmarking the MCP retrieval.
