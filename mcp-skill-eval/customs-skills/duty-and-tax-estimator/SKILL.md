---
name: duty-and-tax-estimator
description: >-
  Estimate the import duty, import VAT, and total landed cost for bringing goods into the UK,
  using live tariff measures. Use whenever someone asks what a shipment will cost in tax or
  duty - "how much duty will I pay on this", "what's the import VAT on X from China", "estimate
  the customs charges", "what's my landed cost", "is it cheaper to import from Vietnam or India",
  "will I pay duty if I import from the EU" - given (or after finding) a commodity code, an
  origin country, a destination (GB or NI), and a customs value. It picks the right duty measure
  for the origin, checks for preference and quotas, and shows the duty + VAT calculation with the
  working. Prefer this over estimating duty rates from memory.
---

# Duty and tax estimator

Estimate what a trader will actually pay to import goods, from live tariff measures - customs
duty, import VAT, and total landed cost - and show the working so they can trust it. Duty rates,
preferences and additional duties change constantly and depend on origin and date, so this must be
grounded in live measures, never recalled from memory.

## Prerequisite - the connector

Needs the **Trade Tariff MCP connector** (`mcp.trade-tariff.service.gov.uk`). Confirm the tools
(`lookup_commodity`, `list_geographical_areas`, `rules_of_origin`, `search_quotas`,
`list_exchange_rates`) are available; if not, stop and tell the user to add it (key from
`https://hub.trade-tariff.service.gov.uk/`). Use `service="uk"` for Great Britain, `service="xi"`
for Northern Ireland - they have different measures.

## What you need from the trader

Gather these (ask only for what's missing):

- **Commodity code** - the 10-digit code. If they don't have one, classify first (use the
  `uk-commodity-code-classifier` skill) - the code drives every number here.
- **Country of origin** - where the goods were made (not necessarily where they ship from). Origin
  decides the duty rate and any preference.
- **Destination** - Great Britain or Northern Ireland.
- **Customs value** and **currency** - normally the CIF value (cost + insurance + freight to the
  UK border). Convert to GBP if needed.
- **Quantity / net mass** - only needed if the duty is specific (per kg/litre/unit) or excise
  applies. Ask once you see a specific or excise measure, not before.

## Method

The full formulas, the VAT base, the GBP 135 low-value rule, and the measure-type cheatsheet are
in `references/duty-and-vat-method.md` - read it before computing. In short:

### 1. Get the measures
`lookup_commodity(commodity_code, service)` returns `import_measures`, each with a `type` (e.g.
"Third country duty", "Tariff preference", "VAT", "Anti-dumping duty"), a `duty` string (e.g.
"6.50 %"), a `geographical_area` (e.g. "ERGA OMNES (1011)" = everywhere, or a specific country/
group), and dates. Read the duty there - do not assume a rate.

### 2. Pick the duty that applies to this origin
- Map the origin with `list_geographical_areas(service)` - countries belong to groups (e.g. a
  GSP/DCTS group, a free-trade-agreement area). A measure matches if its `geographical_area` is
  the origin country or a group containing it.
- A measure for a **specific country/group beats the general "Third country duty" (ERGA OMNES /
  MFN)** measure. The MFN third-country duty is the fallback when no preference applies.

### 3. Check for preference (and its conditions)
- If a **Tariff preference** measure covers the origin, it usually gives a lower (often 0%) rate
  **but requires valid proof of origin**. Call `rules_of_origin(heading_code, country_code,
  service)` for the origin rule and the document needed (e.g. EUR.1, statement on origin / origin
  declaration). Without qualifying origin, the trader pays the MFN rate, so present **both**: "MFN
  X%, or Y% under [agreement] if the goods meet the origin rule and you hold [document]".

### 4. Flag additional duties and quotas
- **Anti-dumping / safeguard / additional duties** are extra and often tied to a supplier-specific
  **additional code** - flag them and tell the trader to confirm their supplier's code, since rates
  differ by manufacturer.
- If a measure references a **quota** (order number), call `search_quotas(order_number, service)`
  to check the quota rate and whether the balance is open - a quota can cut the rate while it lasts.

### 5. VAT and currency
- Take the **VAT** measure's rate (UK standard is 20%; some goods are 5% or 0%). Default to 20% only
  if no VAT measure is present, and say so.
- If the customs value isn't GBP, use `list_exchange_rates(service)` for HMRC's rate to convert.

### 6. Compute (see the method reference for exact formulas)
- **Customs duty** = ad valorem `% x customs value`, or specific `rate x quantity` (needs the unit).
- **Import VAT** = `vat_rate x (customs value + customs duty + excise + incidental costs to the UK
  border)`.
- **Low value:** consignments of customs value <= GBP 135 (and no excise goods) generally have **no
  customs duty**, and VAT is **supply VAT** charged at the point of sale, not import VAT - say this
  if it applies.
- Mention **Postponed VAT Accounting (PVA)** as the usual way to keep import VAT off the cash-at-
  border bill (account for it on the VAT return instead).

## Output

Give a clear breakdown the trader can act on:

- **Customs value** (and the FX rate if converted).
- **Customs duty**: the rate, the measure it came from, and the amount - plus the MFN-vs-preference
  comparison and what proof of origin the preference needs.
- **Excise** (only if alcohol/tobacco/fuel): flag it, with the rate or a clear "needs the specific
  excise rate" note.
- **Import VAT**: the rate, the VAT base (what it's charged on), and the amount.
- **Totals**: cash due at the border vs total landed cost.

## Be honest about what this is

This is a planning **estimate**, not a customs declaration or tax advice. The final bill depends on
the **declared customs value**, **valid proof of origin**, the **correct additional code** (anti-
dumping/safeguard), and the **date of import**. Excise duty (alcohol, tobacco, fuel) and complex
valuation need specialist input - say so rather than guessing. Recommend a customs broker for
high-value or unusual shipments.
